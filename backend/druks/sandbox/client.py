import asyncio
import logging
import shlex
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import asyncssh
import httpx
from drukbox_sdk import Issuer, SandboxAPI, SandboxHost, Secret
from drukbox_sdk.exceptions import (
    SandboxAPIError,
    SandboxNotFoundError,
    SandboxProvisioningError,
    SandboxUnavailableError,
)
from uuid_utils import uuid7

from druks.durable.engine import _step_engine
from druks.harnesses.exceptions import HarnessSandboxProvisioningError
from druks.settings import load_settings

from .constants import SANDBOX_HOST_LEASE_SECONDS
from .exceptions import HostGone, SandboxError, SandboxUnreachable, TemplateNotFound
from .host import Host
from .layout import get_helper_script_path, get_remote_home
from .models import SandboxGrant

logger = logging.getLogger(__name__)

# The exchange answers a refresh request after it fetched the value again, so
# one request takes one fetch round trip.
_REQUEST_TIMEOUT_SECONDS = 5.0

_DRUKS_SANDBOX_LOCAL_SCRIPT = Path(__file__).parent / "druks-sandbox.sh"

# SSH/socket/sandbox errors that mean a fresh VM never became usable.
# CancelledError and programming errors are excluded so they propagate unchanged.
_ACQUIRE_SETUP_REACHABILITY_ERRORS = (
    SandboxError,
    asyncssh.Error,
    OSError,
    TimeoutError,
)


class Client:
    """Ambient client for the drukbox control plane.

    Use the module-level ``sandbox_client`` singleton. Each method reads
    settings on call and manages its own ``SandboxAPI`` lifecycle so
    callers never touch the HTTP layer.
    """

    @asynccontextmanager
    async def ephemeral(
        self,
        *,
        idempotency_key: str | None = None,
        image_override: str | None = None,
        provider: str | None = None,
        sandbox_env: dict[str, str] | None = None,
        secrets: dict[str, Secret | Issuer] | None = None,
        template: str | None = None,
        grant: SandboxGrant | None = None,
    ) -> AsyncIterator[Host]:
        """Acquire, yield, release: for a sandbox bound to one context manager body."""
        host_id: str | None = None

        try:
            async with self.acquire(
                idempotency_key=idempotency_key,
                image_override=image_override,
                provider=provider,
                sandbox_env=sandbox_env,
                secrets=secrets,
                template=template,
                grant=grant,
            ) as host:
                host_id = host.id
                yield host
        finally:
            if host_id:
                await self.release(host_id=host_id)

    @asynccontextmanager
    async def acquire(
        self,
        *,
        idempotency_key: str | None = None,
        image_override: str | None = None,
        provider: str | None = None,
        sandbox_env: dict[str, str] | None = None,
        secrets: dict[str, Secret | Issuer] | None = None,
        template: str | None = None,
        grant: SandboxGrant | None = None,
    ) -> AsyncIterator[Host]:
        """Create a new host (or reuse one matching ``idempotency_key``)
        and yield it with SSH connected. Closes SSH on exit but does NOT
        release the VM — pair with ``release`` for long-lived flows or
        use ``ephemeral`` for one-shots. ``grant`` is the box's credential at
        the issuer: bound to the box once it exists, revoked when no box comes."""
        key = idempotency_key or str(uuid7())
        api = self._api()
        try:
            settings = load_settings()
            image = image_override or settings.sandbox.image
            # Fixed lease: drukbox reaps the host when this lapses, so a run whose
            # worker dies frees its VM without a druks-side reconciler.
            expires_at = datetime.now(UTC) + timedelta(seconds=SANDBOX_HOST_LEASE_SECONDS)
            try:
                try:
                    record = await api.create_host(
                        expires_at=expires_at,
                        env=sandbox_env,
                        idempotency_key=key,
                        image=image or None,
                        provider=provider,
                        secrets=secrets,
                        template=template,
                    )
                except (SandboxProvisioningError, SandboxUnavailableError) as exc:
                    # Transient control-plane failures — a 502 the service raises
                    # when the provider/Tailscale/keyscan step fails, or a
                    # transport/503 SandboxUnavailableError. Classify them into the
                    # in-run retry path so a slow provider window recovers instead
                    # of dead-ending the run. Fatal SDK errors (auth, validation,
                    # conflict, not-found, generic response) are subclasses of the
                    # untouched SandboxAPIError base and fall through unretried.
                    raise HarnessSandboxProvisioningError(
                        f"sandbox host provisioning failed: {exc}"
                    ) from exc
            except BaseException:
                # The next attempt presents a new grant and a new key.
                if grant:
                    await grant.revoke()
                raise
            logger.info("sandbox host created id=%s", record.id)
            if grant:
                await grant.bind(record.id)
            key_path = settings.sandbox_keys_dir / record.id
            if record.private_key:
                settings.sandbox_keys_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
                key_path.write_text(record.private_key)
                key_path.chmod(0o600)
            host = Host(record=record)
            try:
                await _upload_helper_script(host)
            except _ACQUIRE_SETUP_REACHABILITY_ERRORS as error:
                # Fresh VM never became usable; roll back and classify as provisioning.
                await host.aclose()
                await self._best_effort_delete(api, record.id)
                key_path.unlink(missing_ok=True)
                raise HarnessSandboxProvisioningError(
                    f"sandbox host {record.id} unreachable during setup: {error}"
                ) from error
            except BaseException:
                await host.aclose()
                await self._best_effort_delete(api, record.id)
                key_path.unlink(missing_ok=True)
                raise
            try:
                yield host
            finally:
                await host.aclose()
        finally:
            await api.aclose()

    async def list_hosts(self) -> list[SandboxHost]:
        """Every host the control plane has registered, any status."""
        api = self._api()
        try:
            return await api.list_hosts()
        finally:
            await api.aclose()

    async def create_template(self, *, setup_script: str, base_image: str | None, label: str):
        api = self._api()
        try:
            return await api.create_template(
                setup_script=setup_script,
                base_image=base_image,
                label=label,
            )
        finally:
            await api.aclose()

    async def get_template(self, *, base_image: str = "", setup_script_hash: str):
        api = self._api()
        try:
            for template in await api.list_templates():
                if template.setup_script_hash != setup_script_hash:
                    continue
                if base_image and template.base_image != base_image:
                    continue
                return template
        finally:
            await api.aclose()
        raise TemplateNotFound(f"sandbox template {setup_script_hash} does not exist")

    @staticmethod
    async def _revoke_grant(host_id: str) -> None:
        """The denial comes first, and its failure must not stop the cleanup
        behind it."""
        try:
            await SandboxGrant.revoke_for_host(_step_engine(), host_id)
        except Exception:  # noqa: BLE001 — a cleanup surface; log and move on
            logger.exception("failed to revoke the grant of sandbox host %s", host_id)

    @staticmethod
    async def _best_effort_delete(api: SandboxAPI, host_id: str) -> None:
        await Client._revoke_grant(host_id)
        try:
            await api.delete_host(host_id)
        except SandboxNotFoundError:
            pass  # already gone — rollback succeeded
        except (SandboxAPIError, SandboxUnavailableError):
            logger.exception("rollback delete failed for host %s", host_id)

    @asynccontextmanager
    async def attach(self, *, host_id: str) -> AsyncIterator[Host]:
        """Reattach to an existing host. Raises ``HostGone`` if the VM
        has been torn down. SSH closes on exit; the VM stays up."""
        api = self._api()

        try:
            try:
                record = await api.get_host(host_id)
            except SandboxNotFoundError as exc:
                raise HostGone(
                    f"sandbox host {host_id} no longer exists",
                ) from exc
            except SandboxUnavailableError as exc:
                # Transport/503 while looking up an existing host — transient,
                # so classify it the same as a create-time control-plane
                # failure and let the in-run retry re-attach once the service
                # recovers.
                raise HarnessSandboxProvisioningError(
                    f"sandbox host {host_id} lookup failed: {exc}"
                ) from exc
            host = Host(record=record)
            try:
                yield host
            finally:
                await host.aclose()
        finally:
            await api.aclose()

    async def provision(
        self,
        *,
        idempotency_key: str | None = None,
        image_override: str | None = None,
        provider: str | None = None,
        sandbox_env: dict[str, str] | None = None,
        secrets: dict[str, Secret | Issuer] | None = None,
        template: str | None = None,
        grant: SandboxGrant | None = None,
    ) -> Host:
        """Create a host and return its handle without holding an SSH connection —
        the handle reconnects lazily when used (its id and lease expiry are readable
        without one). Caller is responsible for ``release``."""
        async with self.acquire(
            idempotency_key=idempotency_key,
            image_override=image_override,
            provider=provider,
            sandbox_env=sandbox_env,
            secrets=secrets,
            template=template,
            grant=grant,
        ) as host:
            return host

    async def reattach(self, *, host_id: str) -> Host:
        """The handle of a box a crashed process left behind, found through its
        grant. A box that is gone loses the grant, and the retry provisions anew."""
        try:
            async with self.attach(host_id=host_id) as host:
                return host
        except HostGone as exc:
            await self._revoke_grant(host_id)
            raise HarnessSandboxProvisioningError(f"sandbox host {host_id} is gone") from exc

    @asynccontextmanager
    async def resume(self, *, host_id: str) -> AsyncIterator[Host]:
        """``ephemeral`` for a box that exists: reattach, then release on exit."""
        host = await self.reattach(host_id=host_id)
        try:
            yield host
        finally:
            await host.aclose()
            await self.release(host_id=host_id)

    async def request_refreshes(self, subscription_id: str, *, except_host_id: str = "") -> None:
        """Tell the exchange to fetch again for every live box on the
        subscription: a rotation ended the value they hold. One attempt per
        box, side by side, and a failure is a log line. The exchange refreshes
        at expiry in any case. The box whose answer carries the new token
        needs no request."""
        boxes = [
            (grant.host_id, service)
            for grant in await SandboxGrant.list_live_for_subscription(subscription_id)
            if grant.host_id != except_host_id
            for service, held in grant.services.items()
            if held == subscription_id
        ]
        exchange_url = load_settings().sandbox.exchange_url.rstrip("/")
        async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT_SECONDS) as client:
            answers = await asyncio.gather(
                *(
                    client.post(f"{exchange_url}/refresh/{host_id}/{service}")
                    for host_id, service in boxes
                ),
                return_exceptions=True,
            )
        for (host_id, service), answer in zip(boxes, answers, strict=True):
            if isinstance(answer, BaseException) or answer.status_code != 200:
                logger.warning(
                    "refresh request for box %s service %s failed: %s", host_id, service, answer
                )

    async def release(self, *, host_id: str) -> None:
        """Terminate the VM. Idempotent and infallible — already-gone hosts
        no-op silently; any other failure is logged but not surfaced so
        cleanup paths don't have to handle SDK errors at every call site.
        The box's grant dies first, so the denial never waits on the VM."""
        api = self._api()
        settings = load_settings()

        try:
            await self._revoke_grant(host_id)
            try:
                await api.delete_host(host_id)
            except SandboxNotFoundError:
                pass
            except Exception:  # noqa: BLE001 — release is a "never-raises" cleanup surface; log and move on
                logger.exception(
                    "failed to delete sandbox host %s",
                    host_id,
                )
            key_path = settings.sandbox_keys_dir / host_id
            try:
                key_path.unlink(missing_ok=True)
            except OSError as error:
                logger.warning("failed to unlink sandbox key %s: %s", key_path, error)
        finally:
            await api.aclose()

    def _api(self) -> SandboxAPI:
        settings = load_settings()
        return SandboxAPI(
            base_url=settings.sandbox.service_url,
            token=settings.sandbox.service_token,
            timeout=settings.sandbox.timeout,
        )


def provisioning_key(*parts: str) -> str:
    return ":".join(part for part in parts if part)


async def _upload_helper_script(host: Host) -> None:
    helper_path = get_helper_script_path(host.ssh_username)
    await host.upload_file(
        local=_DRUKS_SANDBOX_LOCAL_SCRIPT,
        remote=helper_path,
    )
    await host.exec(["chmod", "755", helper_path], timeout=10.0)

    # Direct .gitconfig write — scope helper to github.com so it never
    # intercepts auth for other hosts. The ``!`` tells git to run the
    # value as a shell command rather than appending it to
    # ``git credential-``.
    gitconfig_path = f"{get_remote_home(host.ssh_username)}/.gitconfig"
    gitconfig_body = (
        f'[credential "https://github.com"]\n\thelper = !{helper_path} git-credential\n'
    )
    write_cmd = f"printf %s {shlex.quote(gitconfig_body)} > {shlex.quote(gitconfig_path)}"
    write_result = await host.exec(
        ["sh", "-c", write_cmd],
        timeout=10.0,
    )
    if not write_result.ok:
        raise SandboxUnreachable(
            f"failed to write {gitconfig_path}: "
            f"exit={write_result.exit_code} stderr={write_result.stderr.strip()}",
        )


sandbox_client = Client()

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import asyncssh
from drukbox_sdk import Issuer, SandboxAPI, SandboxHost, Secret
from drukbox_sdk.exceptions import (
    SandboxAPIError,
    SandboxNotFoundError,
    SandboxProvisioningError,
    SandboxUnavailableError,
)
from sqlalchemy.ext.asyncio import AsyncSession
from uuid_utils import uuid7

from druks.durable.engine import _step_engine
from druks.harnesses.exceptions import HarnessSandboxProvisioningError
from druks.settings import load_settings

from .constants import SANDBOX_HOST_LEASE_SECONDS
from .exceptions import HostGone, SandboxError, TemplateNotFound
from .host import Host
from .layout import get_chat_bridge_path, get_helper_script_path
from .models import SandboxIdentity

logger = logging.getLogger(__name__)

_DRUKS_SANDBOX_LOCAL_SCRIPT = Path(__file__).parent / "druks-sandbox.sh"

# Errors that mean a fresh VM never became usable. Cancellation and
# programming errors propagate unchanged.
_ACQUIRE_SETUP_REACHABILITY_ERRORS = (
    SandboxError,
    asyncssh.Error,
    OSError,
    TimeoutError,
)


class Client:
    """The drukbox control plane. Use the ``sandbox_client`` singleton; each
    method opens and closes its own ``SandboxAPI``."""

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
        identity: SandboxIdentity | None = None,
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
                identity=identity,
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
        identity: SandboxIdentity | None = None,
    ) -> AsyncIterator[Host]:
        """Create a host and yield it with SSH connected. Exit closes SSH, not
        the VM. ``identity`` binds to the box, or dies when no box comes."""
        key = idempotency_key or str(uuid7())
        api = self._api()
        try:
            settings = load_settings()
            image = image_override or settings.sandbox.image
            # drukbox reaps the host at lease end, so a dead worker still frees its VM.
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
                    # A 502 or 503 from the control plane is transient: the run
                    # retries. Every other SDK error is fatal and passes through.
                    raise HarnessSandboxProvisioningError(
                        f"sandbox host provisioning failed: {exc}"
                    ) from exc
            except BaseException:
                # The next attempt presents a new identity and a new key.
                if identity:
                    await identity.revoke()
                raise
            logger.info("sandbox host created id=%s", record.id)
            if identity:
                await identity.bind(record.id)
            key_path = settings.sandbox_keys_dir / record.id
            if record.private_key:
                settings.sandbox_keys_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
                key_path.write_text(record.private_key)
                key_path.chmod(0o600)
            host = Host(record=record)
            try:
                await _upload_helper_script(host)
            except _ACQUIRE_SETUP_REACHABILITY_ERRORS as error:
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
    async def _revoke_identity(host_id: str) -> None:
        """The denial comes first, and its failure must not stop the cleanup
        behind it."""
        try:
            await SandboxIdentity.revoke_for_host(_step_engine(), host_id)
        except Exception:  # noqa: BLE001 — never raises
            logger.exception("failed to revoke the identity of sandbox host %s", host_id)

    @staticmethod
    async def _best_effort_delete(api: SandboxAPI, host_id: str) -> None:
        await Client._revoke_identity(host_id)
        try:
            await api.delete_host(host_id)
        except SandboxNotFoundError:
            pass
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
                # Transient: the run retries and reattaches once the service recovers.
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
        identity: SandboxIdentity | None = None,
    ) -> Host:
        """Create a host and return a handle that connects lazily. The caller
        owns ``release``."""
        async with self.acquire(
            idempotency_key=idempotency_key,
            image_override=image_override,
            provider=provider,
            sandbox_env=sandbox_env,
            secrets=secrets,
            template=template,
            identity=identity,
        ) as host:
            return host

    async def reattach(self, *, host_id: str) -> Host:
        """The handle of a box a crashed process left behind, found through its
        identity. A box that is gone loses it, and the retry provisions anew."""
        try:
            async with self.attach(host_id=host_id) as host:
                return host
        except HostGone as exc:
            await self._revoke_identity(host_id)
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

    async def request_refreshes(
        self, session: AsyncSession, secret_id: str, *, except_host_id: str = ""
    ) -> None:
        """Order a refresh on every live box of the secret, except the one whose
        answer carries the new token. A failure is a log line."""
        boxes = [
            (identity.host_id, ref.name)
            for identity in await SandboxIdentity.list_for_secret(session, secret_id)
            if identity.host_id != except_host_id
            for ref in identity.secret_refs
            if ref.secret_id == secret_id
        ]
        api = self._api()
        try:
            answers = await asyncio.gather(
                *(api.refresh_secret(host_id, service) for host_id, service in boxes),
                return_exceptions=True,
            )
        finally:
            await api.aclose()
        for (host_id, service), answer in zip(boxes, answers, strict=True):
            if isinstance(answer, BaseException):
                logger.warning("refresh of %s on box %s failed: %s", service, host_id, answer)

    async def set_expiry(self, *, host_id: str, expires_at: datetime) -> None:
        """Set the box lease. The SDK raises if the box does not exist."""
        api = self._api()
        try:
            await api.renew_host(host_id, expires_at=expires_at)
        finally:
            await api.aclose()

    async def release(self, *, host_id: str) -> None:
        """Terminate the VM; never raises. The identity dies first, so the
        denial never waits on the VM."""
        api = self._api()
        settings = load_settings()

        try:
            await self._revoke_identity(host_id)
            try:
                await api.delete_host(host_id)
            except SandboxNotFoundError:
                pass
            except Exception:  # noqa: BLE001 — never raises
                logger.exception("failed to delete sandbox host %s", host_id)
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
    await host.upload_file(
        local=Path(__file__).parents[1] / "chat" / "bridge.mjs",
        remote=get_chat_bridge_path(host.ssh_username),
    )


sandbox_client = Client()

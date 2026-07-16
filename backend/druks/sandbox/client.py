import logging
import shlex
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

from drukbox_sdk import SandboxAPI, SandboxHost
from drukbox_sdk.exceptions import (
    SandboxAPIError,
    SandboxNotFoundError,
    SandboxUnavailableError,
)
from uuid_utils import uuid7

from druks.settings import load_settings

from .constants import SANDBOX_HOST_LEASE_SECONDS
from .exceptions import HostGone, SandboxUnreachable
from .host import Sandbox
from .layout import get_helper_script_path, get_remote_home

logger = logging.getLogger(__name__)

_DRUKS_SANDBOX_LOCAL_SCRIPT = Path(__file__).parent / "druks-sandbox.sh"


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
        sandbox_env: dict[str, str] | None = None,
    ) -> AsyncIterator[Sandbox]:
        """One-shot lifecycle: acquire → yield → release. For callers
        whose sandbox is bound to a single context manager body."""
        host_id: str | None = None

        try:
            async with self.acquire(
                idempotency_key=idempotency_key,
                image_override=image_override,
                sandbox_env=sandbox_env,
            ) as sandbox:
                host_id = sandbox.id
                yield sandbox
        finally:
            if host_id:
                await self.release(host_id=host_id)

    @asynccontextmanager
    async def acquire(
        self,
        *,
        idempotency_key: str | None = None,
        image_override: str | None = None,
        sandbox_env: dict[str, str] | None = None,
    ) -> AsyncIterator[Sandbox]:
        """Create a new host (or reuse one matching ``idempotency_key``)
        and yield it with SSH connected. Closes SSH on exit but does NOT
        release the VM — pair with ``release`` for long-lived flows or
        use ``ephemeral`` for one-shots."""
        key = idempotency_key or str(uuid7())
        api = self._api()
        try:
            settings = load_settings()
            image = image_override or settings.sandbox_image
            # Fixed lease: drukbox reaps the host when this lapses, so a run whose
            # worker dies frees its VM without a druks-side reconciler.
            expires_at = datetime.now(UTC) + timedelta(seconds=SANDBOX_HOST_LEASE_SECONDS)
            record = await api.create_host(
                expires_at=expires_at,
                env=sandbox_env,
                idempotency_key=key,
                image=image or None,
            )
            logger.info("sandbox host created id=%s", record.id)
            key_path = settings.sandbox_keys_dir / record.id
            if record.private_key:
                settings.sandbox_keys_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
                key_path.write_text(record.private_key)
                key_path.chmod(0o600)
            sandbox = Sandbox(record=record)
            try:
                await _upload_helper_script(sandbox)
            except BaseException:
                # Caller never sees this host; release rather than orphan.
                await sandbox.aclose()
                await self._best_effort_delete(api, record.id)
                key_path.unlink(missing_ok=True)
                raise
            try:
                yield sandbox
            finally:
                await sandbox.aclose()
        finally:
            await api.aclose()

    async def list_hosts(self) -> list[SandboxHost]:
        """Every host the control plane has registered, any status."""
        api = self._api()
        try:
            return await api.list_hosts()
        finally:
            await api.aclose()

    @staticmethod
    async def _best_effort_delete(api: SandboxAPI, host_id: str) -> None:
        try:
            await api.delete_host(host_id)
        except SandboxNotFoundError:
            pass  # already gone — rollback succeeded
        except (SandboxAPIError, SandboxUnavailableError):
            logger.exception("rollback delete failed for host %s", host_id)

    @asynccontextmanager
    async def attach(self, *, host_id: str) -> AsyncIterator[Sandbox]:
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
            sandbox = Sandbox(record=record)
            try:
                yield sandbox
            finally:
                await sandbox.aclose()
        finally:
            await api.aclose()

    async def provision(
        self,
        *,
        idempotency_key: str | None = None,
        image_override: str | None = None,
        sandbox_env: dict[str, str] | None = None,
    ) -> Sandbox:
        """Create a host and return its handle without holding an SSH connection —
        the handle reconnects lazily when used (its id and lease expiry are readable
        without one). Caller is responsible for ``release``."""
        async with self.acquire(
            idempotency_key=idempotency_key,
            image_override=image_override,
            sandbox_env=sandbox_env,
        ) as sandbox:
            return sandbox

    async def release(self, *, host_id: str) -> None:
        """Terminate the VM. Idempotent and infallible — already-gone hosts
        no-op silently; any other failure is logged but not surfaced so
        cleanup paths don't have to handle SDK errors at every call site."""
        api = self._api()
        settings = load_settings()

        try:
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
            base_url=settings.sandbox_service_url,
            token=settings.sandbox_service_token,
            timeout=settings.sandbox_service_timeout,
        )


async def _upload_helper_script(sandbox: Sandbox) -> None:
    helper_path = get_helper_script_path(sandbox.ssh_username)
    await sandbox.upload_file(
        local=_DRUKS_SANDBOX_LOCAL_SCRIPT,
        remote=helper_path,
    )
    await sandbox.exec(["chmod", "755", helper_path], timeout=10.0)

    # Direct .gitconfig write — scope helper to github.com so it never
    # intercepts auth for other hosts. The ``!`` tells git to run the
    # value as a shell command rather than appending it to
    # ``git credential-``.
    gitconfig_path = f"{get_remote_home(sandbox.ssh_username)}/.gitconfig"
    gitconfig_body = (
        f'[credential "https://github.com"]\n\thelper = !{helper_path} git-credential\n'
    )
    write_cmd = f"printf %s {shlex.quote(gitconfig_body)} > {shlex.quote(gitconfig_path)}"
    write_result = await sandbox.exec(
        ["sh", "-c", write_cmd],
        timeout=10.0,
    )
    if not write_result.ok:
        raise SandboxUnreachable(
            f"failed to write {gitconfig_path}: "
            f"exit={write_result.exit_code} stderr={write_result.stderr.strip()}",
        )


sandbox_client = Client()

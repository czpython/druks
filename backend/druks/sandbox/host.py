import asyncio
import contextlib
import json
import logging
import shlex
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING, Any, Self

import asyncssh
from drukbox_sdk import SandboxHost as SandboxHostRecord
from drukbox_sdk.exceptions import SandboxAPIError, SandboxUnavailableError

from druks.core.utils.time import ensure_utc
from druks.harnesses.artifacts import persist_manifest, persist_prompt, read_cost
from druks.harnesses.exceptions import (
    HarnessError,
    HarnessFirstByteTimeoutError,
    HarnessTimeoutError,
)
from druks.settings import load_settings

from .datastructures import (
    AgentInvocation,
    AgentResult,
    Credentials,
    ExecResult,
    HarnessRunResult,
    McpServer,
)
from .exceptions import SandboxError
from .layout import get_work_root

if TYPE_CHECKING:
    from druks.harnesses.base import Harness

    from .runner import Exec

logger = logging.getLogger(__name__)

# Files the runner writes that we always pull back. Missing files are
# not fatal — a killed-early run may not have an exit_code. Callers can
# supply extras for CLI-specific files (e.g. codex's output.json).
_ARTIFACT_FILENAMES = ("stdout.jsonl", "stderr.log", "exit_code", "pid")

STDOUT_BUFFER_LIMIT = 50 * 1024 * 1024
STDERR_BUFFER_LIMIT = 10 * 1024 * 1024

# Maximum window between exec start and the first output byte before we
# consider the run wedged and kill it. Anthropic's Claude CLI streams its
# first ``stream-json`` event within a couple of seconds normally; the
# worst legitimate cold-start we've measured is well under 60s. 90s gives
# a comfortable margin for slow MCP loads + auth refresh + long-prompt
# assembly while still recovering the operation budget fast when the CLI
# silently wedges.
#
# Pre-LLM wedges (Claude Code's ``--add-dir <cwd>`` event-loop hang,
# upstream HTTP stalls that the CLI doesn't surface as errors) all share
# the symptom "process alive, zero output bytes, forever." The full
# per-operation timeout is in the tens of minutes, so without this guard
# a wedged run blocks a worker slot for far longer than necessary. Pair
# with ``--debug-file`` on the CLI side to get post-mortem context on
# what the wedge was doing.
FIRST_BYTE_KILL_SECONDS_DEFAULT = 90


_CONNECT_TIMEOUT_SECONDS = 30.0
_KEEPALIVE_INTERVAL_SECONDS = 15.0


class Sandbox:
    def __init__(
        self,
        *,
        record: SandboxHostRecord,
    ) -> None:
        self.record = record
        self._conn: asyncssh.SSHClientConnection | None = None
        # Concurrent first-callers can both observe ``self._conn is
        # None`` and race to create two connections, leaking one.
        # Serialize the connect path with a lazy-allocated lock.
        self._conn_lock: asyncio.Lock | None = None

    @property
    def id(self) -> str:
        return self.record.id

    @property
    def host_id(self) -> str:
        # The host an agent call runs on; for a bare sandbox that is its own id.
        return self.id

    @property
    def expires_at(self) -> datetime | None:
        # When drukbox's lease on this host lapses; None if the record carries no lease.
        raw = self.record.expires_at
        return ensure_utc(datetime.fromisoformat(raw)) if raw else None

    @property
    def ssh_username(self) -> str:
        return self.record.ssh_username

    # File transfer

    async def upload_file(
        self,
        *,
        local: Path,
        remote: str,
        mode: int = 0o600,
    ) -> None:
        """SFTP-put ``local`` to ``remote``, mkdir-p its parent, chmod
        to ``mode``. The default mode matches credential files (operator
        wants tight perms by default)."""
        parent = remote.rsplit("/", 1)[0] if "/" in remote.lstrip("/") else ""
        if parent:
            await self.exec(["mkdir", "-p", parent], timeout=10.0)
        conn = await self._ensure_conn()
        async with conn.start_sftp_client() as sftp:
            await sftp.put(str(local), remote)
        await self.exec(["chmod", f"{mode:o}", remote], timeout=10.0)

    async def write_secret(
        self,
        *,
        remote: str,
        secret: str,
        mode: int = 0o600,
    ) -> None:
        """Write ``secret`` directly to ``remote`` without staging a
        local file. Used for short-lived credentials (github tokens,
        etc.) — keeps the secret out of the host filesystem."""
        parent = remote.rsplit("/", 1)[0] if "/" in remote.lstrip("/") else ""
        quoted_secret = shlex.quote(secret)
        quoted_remote = shlex.quote(remote)
        parts: list[str] = []
        if parent:
            parts.append(f"mkdir -p {shlex.quote(parent)}")
        parts.append(f"printf %s {quoted_secret} > {quoted_remote}")
        parts.append(f"chmod {mode:o} {quoted_remote}")
        result = await self.exec(["sh", "-c", " && ".join(parts)], timeout=10.0)
        # Fail loudly — this now carries the harness OAuth credentials, so a
        # silent write failure would start the agent unauthenticated instead of
        # failing the run. The secret never rides the message (only the path).
        if not result.ok:
            raise SandboxError(
                f"failed to write secret to {remote}: "
                f"exit={result.exit_code} {result.stderr.strip()}"
            )

    async def download(self, *, remote: str, local: Path) -> None:
        conn = await self._ensure_conn()
        local.parent.mkdir(parents=True, exist_ok=True)
        async with conn.start_sftp_client() as sftp:
            await sftp.get(remote, str(local))

    async def upload_dir(
        self,
        *,
        local: Path,
        remote: str,
        excludes: Sequence[str] = (),
    ) -> None:
        conn = await self._ensure_conn()
        await self.exec(["mkdir", "-p", remote], timeout=30.0)

        # Remote: untar from stdin into ``remote``. ``-m`` skips mtime
        # restore to avoid clock-skew warnings.
        remote_cmd = f"tar -xmf - -C {shlex.quote(remote)}"
        process = await conn.create_process(remote_cmd, encoding=None)
        try:
            await _stream_local_tar_into(
                local=local,
                excludes=excludes,
                writer=process.stdin,
            )
        finally:
            completed = await process.wait()
        if completed.exit_status:
            raise RuntimeError(
                f"remote tar exited {completed.exit_status} while extracting into {remote}",
            )

    # One-shot exec

    async def run_agent(
        self,
        *,
        model: str,
        prompt: str,
        schema: dict[str, Any],
        agent: str,
        artifact_dir: Path,
        call_id: str | None = None,
        effort: str | None = None,
        timeout: int | None = None,
        github_token: str | None = None,
        include_plugins: bool = True,
        add_dirs: tuple[str, ...] = (),
        extra_env: dict[str, Any] | None = None,
        mcp_servers: tuple[McpServer, ...] = (),
        login_id: str | None = None,
    ) -> AgentResult:
        """Run the harness and return a pure ``AgentResult`` — no database write.
        A failure is captured on the result (``status=FAILED``), not raised.

        The repo is a *precondition*, not an input: callers that need one clone
        it into the VM first (see build's workspace). ``include_plugins=False`` (Claude only)
        skips uploading the operator's plugin state — for prompts that hit no MCP
        server; a no-op for codex.
        """
        # cycle: the harnesses package eagerly imports claude/codex, which
        # import this package's siblings — so the factory can't load while
        # druks.sandbox is mid-init. user_settings is heavy enough
        # that we keep it out of module init either way.
        from druks.durable.enums import AgentCallStatus
        from druks.harnesses.datastructures import SandboxSettings
        from druks.harnesses.registry import get_harness_for_model
        from druks.user_settings.models import HarnessSettings

        settings = load_settings()
        # Effort/timeout fall back to the model's harness defaults.
        harness_class = get_harness_for_model(model)
        harness_settings = HarnessSettings.require(harness_class.name)
        effort = effort or harness_settings.effort
        timeout = timeout if timeout is not None else harness_settings.timeout
        harness = harness_class(
            model=model,
            fast_mode=harness_settings.fast_mode,
            effort=effort,
            sandbox=SandboxSettings.maybe_from_settings(settings),
        )

        # Names the artifact subdir and is the AgentCall.id — supplied by the
        # orchestrator so the row already exists (RUNNING) before we run; falls
        # back to a fresh uuid for callers that record the call after the fact.
        run_id = harness.mint_run_id(call_id)
        started_at = datetime.now(UTC)
        status = AgentCallStatus.SUCCEEDED
        last_error: str | None = None
        output: Any = None
        try:
            output = await self.run_prompt(
                harness,
                prompt=prompt,
                schema=schema,
                artifact_dir=artifact_dir,
                timeout=timeout,
                github_token=github_token,
                include_plugins=include_plugins,
                add_dirs=add_dirs,
                extra_env=extra_env,
                mcp_servers=mcp_servers,
                call_id=run_id,
                login_id=login_id,
            )
        except Exception as exc:  # noqa: BLE001 — captured on the result, not raised
            status = AgentCallStatus.FAILED
            last_error = f"{agent}: {type(exc).__name__}: {exc}"
        cost_usd, cost_metadata = read_cost(artifact_dir / run_id)
        return AgentResult(
            output=output,
            run_id=run_id,
            sandbox_host_id=self.id,
            model=model,
            agent=agent,
            status=status,
            started_at=started_at,
            cost_usd=cost_usd,
            cost_metadata=cost_metadata,
            last_error=last_error,
        )

    async def run_prompt(
        self,
        harness: "Harness",
        *,
        prompt: str,
        schema: dict[str, Any],
        artifact_dir: Path,
        timeout: int,
        github_token: str | None = None,
        include_plugins: bool = True,
        add_dirs: tuple[str, ...] = (),
        extra_env: dict[str, str] | None = None,
        mcp_servers: tuple[McpServer, ...] = (),
        call_id: str | None = None,
        login_id: str | None = None,
    ) -> Any:
        """Drive one prompt through ``harness`` on this VM: the harness
        builds the invocation and parses the result; this sandbox executes it.
        One-shot callers with a hand-built harness use this directly;
        ``run_agent`` adds the harness factory + cost capture on top."""
        run_id = harness.mint_run_id(call_id)
        artifact_dir.mkdir(parents=True, exist_ok=True)
        persist_prompt(artifact_dir, call_id=run_id, prompt=prompt)
        persist_manifest(
            artifact_dir,
            call_id=run_id,
            manifest=harness.get_manifest(mcp_servers=mcp_servers, extra_env=extra_env),
        )

        invocation = harness.build_invocation(
            prompt=prompt,
            schema=schema,
            run_id=run_id,
            ssh_username=self.ssh_username,
            github_token=github_token,
            include_plugins=include_plugins,
            add_dirs=add_dirs,
            extra_env=extra_env,
            mcp_servers=mcp_servers,
            login_id=login_id,
        )
        result = await self._exec(
            invocation,
            run_id=run_id,
            artifact_dir=artifact_dir,
            timeout=timeout,
        )
        return harness.parse(result, artifact_dir=artifact_dir, run_id=run_id)

    async def _exec(
        self,
        invocation: AgentInvocation,
        *,
        run_id: str,
        artifact_dir: Path,
        timeout: int,
        first_byte_kill_seconds: int | None = FIRST_BYTE_KILL_SECONDS_DEFAULT,
    ) -> HarnessRunResult:
        """Execute a built invocation on this VM: tee stdout/stderr to the
        artifact dir, enforce the first-byte and overall timeouts, record
        ``metadata.json``, download artifacts."""
        local_dir = artifact_dir / run_id
        local_dir.mkdir(parents=True, exist_ok=True)
        stdout_log = local_dir / "stdout.jsonl"
        stderr_log = local_dir / "stderr.log"
        metadata_path = local_dir / "metadata.json"

        stdout_buf = bytearray()
        stderr_buf = bytearray()
        started_at = datetime.now(UTC)
        timed_out = False
        first_byte_killed = False

        try:
            run = await self._start_instruction(
                credentials_bundle=invocation.credentials,
                cmd=list(invocation.args),
                run_id=run_id,
                extra_env=invocation.env,
                cwd=invocation.cwd,
                stdin_data=invocation.stdin,
            )
            try:
                _write_metadata(
                    metadata_path,
                    {
                        "name": invocation.name,
                        "sandbox_host_id": run.host.id,
                        "sandbox_run_id": run.run_id,
                        "started_at": started_at.isoformat(),
                        "timeout_seconds": timeout,
                        # The exact CLI args the sandbox executed. Credentials
                        # don't flow through args (they go via the separate
                        # ``credentials`` env-injection path), so this is safe
                        # to persist verbatim. Lets an operator ssh into the
                        # sandbox and reproduce a failed call by hand without
                        # spelunking source to reconstruct flags.
                        "args": list(invocation.args),
                        "cwd": invocation.cwd,
                    },
                )

                with open(stdout_log, "wb") as out_f, open(stderr_log, "wb") as err_f:
                    tee_out = asyncio.create_task(
                        _tee_stream(
                            run.tail("stdout"),
                            out_f,
                            stdout_buf,
                            STDOUT_BUFFER_LIMIT,
                        ),
                    )
                    tee_err = asyncio.create_task(
                        _tee_stream(
                            run.tail("stderr"),
                            err_f,
                            stderr_buf,
                            STDERR_BUFFER_LIMIT,
                        ),
                    )

                    # First-byte kill: cancel the run if it emits nothing at all
                    # within the window — the symptom of a wedge before the agent
                    # gets going. "Nothing" means neither stdout nor stderr: codex
                    # streams its progress on stderr and returns its result via the
                    # --output-last-message file, so its stdout stays empty for a
                    # healthy run; watching stdout alone would kill every codex.
                    first_byte_task: asyncio.Task[bool] | None = None
                    if first_byte_kill_seconds is not None:
                        first_byte_task = asyncio.create_task(
                            _first_byte_killer(
                                run=run,
                                stdout_buf=stdout_buf,
                                stderr_buf=stderr_buf,
                                delay_seconds=first_byte_kill_seconds,
                            ),
                        )

                    try:
                        try:
                            returncode = await asyncio.wait_for(run.wait(), timeout=timeout)
                        except TimeoutError:
                            timed_out = True
                            with contextlib.suppress(Exception):
                                await run.kill()
                            # Wait briefly for the helper to write its exit code
                            # post-kill; otherwise returncode stays -1.
                            try:
                                returncode = await asyncio.wait_for(run.wait(), timeout=10.0)
                            except Exception:
                                returncode = -1
                    except BaseException:
                        # The wait failed some other way (SSH dropped, cancelled):
                        # the tails may never see an exit code, so cancel instead
                        # of draining — the finally's gather reaps them before the
                        # log files close.
                        tee_out.cancel()
                        tee_err.cancel()
                        raise
                    finally:
                        if first_byte_task:
                            if first_byte_task.done() and not first_byte_task.cancelled():
                                with contextlib.suppress(Exception):
                                    first_byte_killed = first_byte_task.result()
                            else:
                                first_byte_task.cancel()
                                with contextlib.suppress(asyncio.CancelledError, Exception):
                                    await first_byte_task

                        # Tail tasks finish when the run.wait() completes
                        # (tail terminates on exit_code), but we still wait
                        # to drain remaining bytes.
                        await asyncio.gather(tee_out, tee_err, return_exceptions=True)
            finally:
                # Best-effort artifact download however the run ended —
                # never mask a downstream error with a download failure.
                try:
                    await self._download_artifacts(
                        run,
                        local_dir,
                        extra_filenames=invocation.extra_artifact_filenames,
                    )
                except Exception:  # noqa: BLE001 — best-effort cleanup in finally; never mask the caller's error
                    logger.exception("artifact download failed for run %s", run.run_id)
        except SandboxError as exc:
            # SSH unreachable, host gone, sandbox-service down. Translate
            # into the existing HarnessError taxonomy so callers don't
            # have to learn a new exception family.
            raise HarnessError(f"{invocation.name} sandbox failure: {exc}") from exc

        ended_at = datetime.now(UTC)
        elapsed = (ended_at - started_at).total_seconds()
        _write_metadata(
            metadata_path,
            {
                "name": invocation.name,
                "sandbox_host_id": run.host.id,
                "sandbox_run_id": run.run_id,
                "started_at": started_at.isoformat(),
                "ended_at": ended_at.isoformat(),
                "elapsed_seconds": round(elapsed, 1),
                "exit_code": returncode,
                "timeout_seconds": timeout,
                "timed_out": timed_out,
                "first_byte_killed": first_byte_killed,
                "first_byte_kill_seconds": first_byte_kill_seconds,
                # See the initial metadata write for the rationale on persisting
                # the literal CLI args.
                "args": list(invocation.args),
                "cwd": invocation.cwd,
            },
        )

        if first_byte_killed:
            raise HarnessFirstByteTimeoutError(
                f"{invocation.name} produced no output within {first_byte_kill_seconds}s; killed.",
            )

        if timed_out:
            raise HarnessTimeoutError(f"{invocation.name} timed out after {timeout}s.")

        return HarnessRunResult(
            returncode=int(returncode),
            stdout=bytes(stdout_buf),
            stderr=bytes(stderr_buf),
        )

    async def exec(
        self,
        cmd: list[str],
        *,
        timeout: float = 30.0,
    ) -> ExecResult:
        conn = await self._ensure_conn()
        joined = " ".join(shlex.quote(part) for part in cmd)
        completed = await conn.run(joined, check=False, timeout=timeout)
        # asyncssh's returncode is the exit status, or the negative signal number when
        # the command was killed (an OOM'd clone dies on SIGKILL → -9), or None if the
        # channel closed with neither. A signal death must read as failure — coercing it
        # to 0 let callers proceed against a half-cloned workspace.
        returncode = completed.returncode
        return ExecResult(
            exit_code=returncode if returncode is not None else -1,
            stdout=_as_str(completed.stdout),
            stderr=_as_str(completed.stderr),
        )

    # Instruction lifecycle

    async def _start_instruction(
        self,
        *,
        credentials_bundle: Credentials,
        cmd: list[str],
        run_id: str,
        extra_env: dict[str, str] | None = None,
        cwd: str | None = None,
        stdin_data: bytes | None = None,
    ) -> "Exec":
        # cycle: each sibling imports Sandbox from this module
        from . import credentials as _credentials
        from . import runner as _runner

        await _credentials.push(self, credentials_bundle)

        # Any repo the agent reads was cloned into the VM before this exec;
        # the run itself only needs a working dir.
        cwd_resolved = cwd or get_work_root(self.ssh_username)
        await self.exec(["mkdir", "-p", cwd_resolved], timeout=10.0)

        return await _runner.start_exec(
            host=self,
            run_id=run_id,
            cmd=cmd,
            cwd=cwd_resolved,
            env=extra_env,
            stdin_data=stdin_data,
        )

    async def _download_artifacts(
        self,
        run: "Exec",
        local_dir: Path,
        *,
        extra_filenames: tuple[str, ...] = (),
    ) -> None:
        local_dir.mkdir(parents=True, exist_ok=True)
        for filename in (*_ARTIFACT_FILENAMES, *extra_filenames):
            remote_path = f"{run.run_dir}/{filename}"
            local_path = local_dir / filename
            try:
                await self.download(remote=remote_path, local=local_path)
            except (asyncssh.SFTPError, OSError):
                # Absence is common (exit_code on a killed run, pid on a
                # never-started wrapper).
                logger.debug("artifact missing or unreadable: %s", remote_path)

    # Connection lifecycle

    async def ssh_connection(self) -> asyncssh.SSHClientConnection:
        return await self._ensure_conn()

    async def aclose(self) -> None:
        if not self._conn:
            return
        self._conn.close()
        await self._conn.wait_closed()
        self._conn = None

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    # Internals

    async def _ensure_conn(self) -> asyncssh.SSHClientConnection:
        if self._conn:
            return self._conn
        if not self._conn_lock:
            self._conn_lock = asyncio.Lock()
        async with self._conn_lock:
            # Re-check under the lock; another coroutine may have raced
            # ahead.
            if self._conn:
                return self._conn
            self._conn = await asyncssh.connect(**self._ssh_connect_kwargs())
            return self._conn

    def _ssh_connect_kwargs(self) -> dict[str, Any]:
        common: dict[str, Any] = {
            "username": self.ssh_username,
            "known_hosts": asyncssh.import_known_hosts(self.record.known_hosts),
            "connect_timeout": _CONNECT_TIMEOUT_SECONDS,
            "keepalive_interval": _KEEPALIVE_INTERVAL_SECONDS,
        }
        if self.record.internal_ssh_host:
            return {
                **common,
                "host": self.record.internal_ssh_host,
                "port": 22,
            }
        if self.record.external_ssh_host:
            # Gate on the FS-persisted key, not record.private_key: the key
            # is returned once, on create — a reattached record (GET) never
            # has it, but the acquirer wrote it to the shared keys dir.
            key_path = load_settings().sandbox_keys_dir / self.record.id
            if key_path.exists():
                return {
                    **common,
                    "host": self.record.external_ssh_host,
                    "port": self.record.external_ssh_port,
                    "client_keys": [str(key_path)],
                }
        raise RuntimeError(
            f"Sandbox {self.record.id}: no reachable address on the record.",
        )


class _TarWriter:
    def write(self, data: bytes) -> None: ...  # pragma: no cover
    async def drain(self) -> None: ...  # pragma: no cover
    def write_eof(self) -> None: ...  # pragma: no cover


async def _stream_local_tar_into(
    *,
    local: Path,
    excludes: Sequence[str],
    writer: _TarWriter,
) -> None:
    exclude_args: list[str] = []
    for pattern in excludes:
        exclude_args.extend(["--exclude", pattern])

    proc = await asyncio.create_subprocess_exec(
        "tar",
        "-chf",
        "-",
        *exclude_args,
        "-C",
        str(local),
        ".",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert proc.stdout is not None

    try:
        while True:
            chunk = await proc.stdout.read(64 * 1024)
            if not chunk:
                break
            writer.write(chunk)
            await writer.drain()
        writer.write_eof()
    finally:
        rc = await proc.wait()

    # tar exit codes: 0 = ok, 1 = "some files differ / changed or were
    # removed while reading" (the archive is still complete minus the
    # vanished file — benign for these best-effort config/skill uploads),
    # 2+ = fatal (e.g. a genuinely unreadable file, which should still
    # surface). Only 2+ fails the run; a churning source dir (e.g. a
    # skills tree being synced on the host) exits 1 and must not take
    # down every agent operation in credentials.push. Observed in prod:
    # "tar: ./<skill>: File removed before we read it" → exit 1.
    if rc >= 2:
        stderr = b""
        if proc.stderr is not None:
            stderr = await proc.stderr.read()
        raise RuntimeError(
            f"local tar exited {rc} while tarring {local}: "
            f"{stderr.decode('utf-8', errors='replace').strip()}",
        )
    if rc == 1:
        stderr = b""
        if proc.stderr is not None:
            stderr = await proc.stderr.read()
        logger.warning(
            "local tar exited 1 (files changed/removed during read) while tarring "
            "%s; uploaded the rest: %s",
            local,
            stderr.decode("utf-8", errors="replace").strip(),
        )


def _as_str(value: object) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value) if value is not None else ""


async def _tee_stream(
    chunks: Any,
    log_file: Any,
    buffer: bytearray,
    buffer_limit: int,
) -> None:
    async for chunk in chunks:
        log_file.write(chunk)
        log_file.flush()
        buffer.extend(chunk)
        if len(buffer) > buffer_limit:
            excess = len(buffer) - buffer_limit
            del buffer[:excess]


async def _first_byte_killer(
    *,
    run: Any,
    stdout_buf: bytearray,
    stderr_buf: bytearray,
    delay_seconds: int,
) -> bool:
    try:
        await asyncio.sleep(delay_seconds)
    except asyncio.CancelledError:
        return False

    if len(stdout_buf) > 0 or len(stderr_buf) > 0:
        return False

    try:
        await run.kill()
    except (SandboxAPIError, SandboxUnavailableError):
        # If the kill RPC fails the overall timeout will eventually surface
        # as HarnessTimeoutError; the killer's "best-effort" contract is
        # explicit about that fallback.
        logger.exception("first-byte killer failed to send kill")
        return False
    return True


def _write_metadata(path: Path, data: dict[str, object]) -> None:
    path.write_text(json.dumps(data, indent=2))

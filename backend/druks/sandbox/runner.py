import asyncio
import shlex
import tempfile
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Literal, NamedTuple

import asyncssh

from .exceptions import ExecFailed, SandboxUnreachable
from .host import Host
from .layout import get_helper_script_path, get_runs_root

# Polling cadence for tail() and wait(). 500ms is the cap on how stale
# the transcript view can look; an operator watching at human speed
# doesn't notice.
POLL_INTERVAL_SECONDS = 0.5

# Reconnect policy: exponential backoff up to ``RECONNECT_BACKOFF_MAX``,
# total budget ``RECONNECT_MAX_DURATION_SECONDS`` before surfacing as
# :class:`SandboxUnreachable`. Five minutes is generous enough that a
# Tailscale rebroadcast or a brief sandbox-service hiccup self-heals;
# anything longer is real and the run is probably orphaned.
RECONNECT_BACKOFF_BASE_SECONDS = 1.0
RECONNECT_BACKOFF_MAX_SECONDS = 30.0
RECONNECT_MAX_DURATION_SECONDS = 300.0

# How long to wait for the helper to write its pid file before we
# decide it never started. Generous because the SSH spawn round-trip
# alone can take a few hundred ms on a fresh Tailnet.
SPAWN_TIMEOUT_SECONDS = 5.0
SPAWN_POLL_INTERVAL_SECONDS = 0.2

Stream = Literal["stdout", "stderr"]


class Completion(NamedTuple):
    done: bool  # the exit_code file exists on the VM
    exit_code: int | None  # the run's exit code, set iff ``done``
    stdout_bytes: int  # size of stdout.jsonl so far (0 ⇒ nothing emitted)

    def __str__(self) -> str:
        code = "…" if self.exit_code is None else self.exit_code
        return f"Completion(done={self.done}, code={code}, stdout={self.stdout_bytes}b)"


class Exec:
    def __init__(
        self,
        *,
        host: Host,
        run_id: str,
        run_dir: str,
    ) -> None:
        self.host = host
        self.run_id = run_id
        self.run_dir = run_dir

    # File paths

    @property
    def stdout_path(self) -> str:
        return f"{self.run_dir}/stdout.jsonl"

    @property
    def stderr_path(self) -> str:
        return f"{self.run_dir}/stderr.log"

    @property
    def pid_path(self) -> str:
        return f"{self.run_dir}/pid"

    @property
    def exit_code_path(self) -> str:
        return f"{self.run_dir}/exit_code"

    def _stream_path(self, stream: Stream) -> str:
        return self.stdout_path if stream == "stdout" else self.stderr_path

    # Streaming

    async def tail(
        self,
        stream: Stream = "stdout",
        *,
        from_offset: int = 0,
    ) -> AsyncIterator[bytes]:
        path = self._stream_path(stream)
        offset = from_offset
        reconnect_start: float | None = None
        attempt = 0
        while True:
            try:
                conn = await self.host.ssh_connection()
                async with conn.start_sftp_client() as sftp:
                    chunk, offset = await self._read_new_bytes(sftp, path, offset)
                    if chunk:
                        yield chunk
                    done = await self._is_done(sftp)
                # Successful poll resets the reconnect window.
                reconnect_start = None
                attempt = 0
                if done:
                    # Final drain — bytes may have been written between
                    # the size check and the exit-code check above.
                    final = await self._final_drain(path, offset)
                    if final:
                        yield final
                    return
                await asyncio.sleep(POLL_INTERVAL_SECONDS)
            except (asyncssh.Error, OSError, ConnectionError) as exc:
                await self.host.aclose()
                now = time.monotonic()
                if reconnect_start is None:
                    reconnect_start = now
                if now - reconnect_start > RECONNECT_MAX_DURATION_SECONDS:
                    raise SandboxUnreachable(
                        f"tail({stream}) on run {self.run_id} unable to "
                        f"reconnect after {RECONNECT_MAX_DURATION_SECONDS}s",
                    ) from exc
                attempt += 1
                await asyncio.sleep(_backoff(attempt))

    async def _read_new_bytes(
        self,
        sftp: asyncssh.SFTPClient,
        path: str,
        offset: int,
    ) -> tuple[bytes, int]:
        size = await self._sftp_size(sftp, path)
        if size <= offset:
            return b"", offset
        async with sftp.open(path, "rb") as fh:
            await fh.seek(offset)
            data = await fh.read(size - offset)
        chunk = data if isinstance(data, bytes) else data.encode("utf-8")
        return chunk, offset + len(chunk)

    async def _final_drain(self, path: str, offset: int) -> bytes:
        conn = await self.host.ssh_connection()
        async with conn.start_sftp_client() as sftp:
            chunk, _ = await self._read_new_bytes(sftp, path, offset)
        return chunk

    async def _sftp_size(self, sftp: asyncssh.SFTPClient, path: str) -> int:
        try:
            attrs = await sftp.stat(path)
        except asyncssh.SFTPNoSuchFile:
            return 0
        return int(attrs.size or 0)

    async def _is_done(self, sftp: asyncssh.SFTPClient) -> bool:
        try:
            await sftp.stat(self.exit_code_path)
        except asyncssh.SFTPNoSuchFile:
            return False
        return True

    # Completion

    async def wait(self) -> int:
        reconnect_start: float | None = None
        attempt = 0
        while True:
            try:
                conn = await self.host.ssh_connection()
                async with conn.start_sftp_client() as sftp:
                    if await self._is_done(sftp):
                        return await self._read_exit_code(sftp)
                reconnect_start = None
                attempt = 0
                await asyncio.sleep(POLL_INTERVAL_SECONDS)
            except (asyncssh.Error, OSError, ConnectionError) as exc:
                await self.host.aclose()
                now = time.monotonic()
                if reconnect_start is None:
                    reconnect_start = now
                if now - reconnect_start > RECONNECT_MAX_DURATION_SECONDS:
                    raise SandboxUnreachable(
                        f"wait() on run {self.run_id} unable to reconnect "
                        f"after {RECONNECT_MAX_DURATION_SECONDS}s",
                    ) from exc
                attempt += 1
                await asyncio.sleep(_backoff(attempt))

    async def _read_exit_code(self, sftp: asyncssh.SFTPClient) -> int:
        async with sftp.open(self.exit_code_path, "rb") as fh:
            raw = await fh.read()
        text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)
        return int(text.strip())

    async def completion(self) -> Completion:
        try:
            conn = await self.host.ssh_connection()
            async with conn.start_sftp_client() as sftp:
                stdout_bytes = await self._sftp_size(sftp, self.stdout_path)
                done = await self._is_done(sftp)
                exit_code = await self._read_exit_code(sftp) if done else None
        except (asyncssh.Error, OSError, ConnectionError) as exc:
            await self.host.aclose()
            raise SandboxUnreachable(
                f"completion() on run {self.run_id} failed to read sandbox state",
            ) from exc

        return Completion(done=done, exit_code=exit_code, stdout_bytes=stdout_bytes)

    # Cancellation

    async def kill(self) -> None:
        await self.host.exec(
            [
                get_helper_script_path(self.host.ssh_username),
                "exec-kill",
                "--run-id",
                self.run_id,
            ],
            timeout=10.0,
        )


async def start_exec(
    *,
    host: Host,
    run_id: str,
    cmd: list[str],
    cwd: str,
    env: dict[str, str] | None = None,
    stdin_data: bytes | None = None,
) -> Exec:
    helper_path = get_helper_script_path(host.ssh_username)
    runs_root_path = get_runs_root(host.ssh_username)
    run_dir = f"{runs_root_path.rstrip('/')}/{run_id}"
    env_file = ""
    stdin_file = ""

    # Stdin-from-file (for prompts too large to embed in the SSH exec
    # command). Upload via SFTP — that's one channel-data stream, no
    # exec-request size limit. Must precede the spawn so the wrapper's
    # ``<$stdin_from`` sees the file.
    if stdin_data is not None:
        stdin_file = f"{run_dir}/stdin"
        await host.exec(["mkdir", "-p", run_dir], timeout=10.0)
        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            tmp.write(stdin_data)
            tmp_path = Path(tmp.name)
        try:
            await host.upload_file(local=tmp_path, remote=stdin_file)
        finally:
            tmp_path.unlink(missing_ok=True)

    # If env was supplied, write it to a file the helper will source.
    # We avoid stuffing env into the SSH command line because quoting
    # through nohup → setsid → sh → the helper is brittle. A file is
    # one round-trip and POSIX-source semantics are obvious.
    if env:
        env_file = f"{run_dir}/env"
        body = "\n".join(_render_env_line(k, v) for k, v in env.items()) + "\n"
        write_cmd = (
            f"mkdir -p {shlex.quote(run_dir)} && "
            f"printf %s {shlex.quote(body)} > {shlex.quote(env_file)}"
        )
        wrote = await host.exec(["sh", "-c", write_cmd], timeout=10.0)
        if not wrote.ok:
            raise ExecFailed(
                f"could not write env file: {wrote.stderr.strip()}",
                exit_code=wrote.exit_code,
            )

    # Build the helper invocation. Detached via ``nohup setsid ... &``
    # so the helper outlives the SSH session.
    args = [helper_path, "exec-start", "--run-id", run_id, "--cwd", cwd]
    if env_file:
        args += ["--env-file", env_file]
    if stdin_file:
        args += ["--stdin-from", stdin_file]
    args += ["--", *cmd]
    quoted = " ".join(shlex.quote(a) for a in args)
    detach = (
        f"nohup setsid {quoted} </dev/null >/dev/null 2>&1 & "
        # ``exit 0`` so exec doesn't see a non-zero from the
        # backgrounding shell when the helper's still starting.
        "exit 0"
    )
    spawn = await host.exec(["sh", "-c", detach], timeout=15.0)
    if not spawn.ok:
        raise ExecFailed(
            f"helper spawn failed: {spawn.stderr.strip()}",
            exit_code=spawn.exit_code,
        )

    # Confirm the helper started by waiting for its pid file to land.
    run = Exec(host=host, run_id=run_id, run_dir=run_dir)
    deadline = time.monotonic() + SPAWN_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        check = await host.exec(
            ["sh", "-c", f"test -s {shlex.quote(run.pid_path)} && cat {shlex.quote(run.pid_path)}"],
            timeout=5.0,
        )
        if check.ok and check.stdout.strip():
            return run
        await asyncio.sleep(SPAWN_POLL_INTERVAL_SECONDS)

    raise ExecFailed(
        f"helper did not write {run.pid_path} within {SPAWN_TIMEOUT_SECONDS}s",
        exit_code=-1,
    )


async def attach(
    *,
    host: Host,
    run_id: str,
) -> Exec:
    runs_root_path = get_runs_root(host.ssh_username)
    run_dir = f"{runs_root_path.rstrip('/')}/{run_id}"
    return Exec(host=host, run_id=run_id, run_dir=run_dir)


def _backoff(attempt: int) -> float:
    return min(
        RECONNECT_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)),
        RECONNECT_BACKOFF_MAX_SECONDS,
    )


def _render_env_line(key: str, value: str) -> str:
    escaped = value.replace("'", "'\\''")
    return f"{key}='{escaped}'"

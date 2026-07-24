import asyncio
from dataclasses import dataclass, field
from typing import Self
from unittest.mock import patch

import asyncssh
import pytest
from druks.sandbox import runner
from druks.sandbox.exceptions import ExecFailed, SandboxUnreachable
from druks.sandbox.host import ExecResult
from druks.sandbox.runner import Exec, attach, start_exec


class _FakeSFTPNoSuchFile(asyncssh.SFTPNoSuchFile):
    def __init__(self, reason: str = "no such file") -> None:
        super().__init__(reason)


@dataclass
class _FakeAttrs:
    size: int


@dataclass
class _FakeFileHandle:
    data: bytes
    _pos: int = 0

    async def seek(self, offset: int) -> None:
        self._pos = offset

    async def read(self, size: int = -1) -> bytes:
        if size == -1:
            chunk = self.data[self._pos :]
            self._pos = len(self.data)
        else:
            chunk = self.data[self._pos : self._pos + size]
            self._pos += len(chunk)
        return chunk

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None


@dataclass
class _FakeVM:
    files: dict[str, bytes] = field(default_factory=dict)

    def write(self, path: str, data: bytes) -> None:
        self.files[path] = data

    def append(self, path: str, data: bytes) -> None:
        self.files[path] = self.files.get(path, b"") + data

    def exists(self, path: str) -> bool:
        return path in self.files


class _FakeSFTP:
    def __init__(self, vm: _FakeVM) -> None:
        self.vm = vm

    async def stat(self, path: str) -> _FakeAttrs:
        if not self.vm.exists(path):
            raise _FakeSFTPNoSuchFile()
        return _FakeAttrs(size=len(self.vm.files[path]))

    def open(self, path: str, extension: str = "rb") -> _FakeFileHandle:
        # asyncssh returns an awaitable that resolves to a file handle
        # supporting async context manager + async read. We collapse
        # that to a directly-returned object because the runner uses
        # ``async with sftp.open(...) as fh``.
        if not self.vm.exists(path):
            raise _FakeSFTPNoSuchFile()
        return _FakeFileHandle(data=self.vm.files[path])

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None


class _FakeConnection:
    def __init__(self, vm: _FakeVM) -> None:
        self.vm = vm
        self.start_calls = 0
        # Optional injection: raise this many times before succeeding.
        self.fail_remaining = 0

    def start_sftp_client(self) -> _FakeSFTP:
        self.start_calls += 1
        if self.fail_remaining > 0:
            self.fail_remaining -= 1
            raise asyncssh.ConnectionLost("connection died")
        return _FakeSFTP(self.vm)


class _FakeSandbox:
    def __init__(
        self,
        vm: _FakeVM | None = None,
        exec_results: list[ExecResult] | None = None,
        ssh_username: str = "root",
    ) -> None:
        self.vm = vm or _FakeVM()
        self.exec_log: list[list[str]] = []
        self.exec_results = exec_results or [ExecResult(0, "", "")]
        self.conn = _FakeConnection(self.vm)
        self.aclose_calls = 0
        # Matches the real ``Sandbox`` interface — the runner reads this
        # to derive the per-user druks-sandbox helper path. Tests pin
        # ``root`` by default so existing path assertions stay simple.
        self.ssh_username = ssh_username

    async def exec(self, cmd: list[str], *, timeout: float = 30.0) -> ExecResult:
        self.exec_log.append(cmd)
        if self.exec_results:
            # FIFO consumption with a sticky-last fallback so callers
            # that don't enumerate every call still get something.
            if len(self.exec_results) > 1:
                return self.exec_results.pop(0)
            return self.exec_results[0]
        return ExecResult(0, "", "")

    async def ssh_connection(self) -> _FakeConnection:
        return self.conn

    async def aclose(self) -> None:
        self.aclose_calls += 1


@pytest.fixture
def fast_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:

    sleeps: list[float] = []
    real_sleep = asyncio.sleep

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        # Yield to the loop cooperatively without going through the
        # patched sleep attribute.
        await real_sleep(0)

    monkeypatch.setattr("druks.sandbox.runner.asyncio.sleep", fake_sleep)
    return sleeps


async def test_start_exec_writes_env_file_and_invokes_druks_sandbox_verb(
    fast_sleep: list[float],
):
    sandbox = _FakeSandbox(
        exec_results=[
            ExecResult(0, "", ""),  # env file write
            ExecResult(0, "", ""),  # spawn (nohup setsid druks-sandbox exec-start ...)
            ExecResult(0, "12345", ""),  # pid check
        ],
    )

    run = await start_exec(
        host=sandbox,  # type: ignore[arg-type]
        run_id="r-42",
        cmd=["claude", "--print", "do the thing"],
        cwd="/work/repo",
        env={"ANTHROPIC_API_KEY_FILE": "/creds/anthropic"},
    )

    assert isinstance(run, Exec)
    assert run.run_id == "r-42"
    assert run.run_dir == "/root/work/runs/r-42"

    env_write, spawn, pid_check = sandbox.exec_log
    # Env-file write: one sh -c that mkdirs + printfs into the env file.
    assert env_write[0] == "sh"
    assert "mkdir -p /root/work/runs/r-42" in env_write[2]
    assert "printf %s" in env_write[2]
    assert "ANTHROPIC_API_KEY_FILE" in env_write[2]
    assert "/creds/anthropic" in env_write[2]
    assert "/root/work/runs/r-42/env" in env_write[2]
    # Spawn: nohup + setsid + druks-sandbox exec-start ..., fully detached.
    assert spawn[0] == "sh"
    assert "nohup setsid" in spawn[2]
    assert "/root/druks-sandbox exec-start" in spawn[2]
    assert "--run-id r-42" in spawn[2]
    assert "--cwd /work/repo" in spawn[2]
    assert "--env-file /root/work/runs/r-42/env" in spawn[2]
    # User command after `--`, with shell-meta-only args getting quoted.
    assert "-- claude --print" in spawn[2]
    assert "'do the thing'" in spawn[2]
    # Pid check.
    assert pid_check[0] == "sh"
    assert "test -s" in pid_check[2] and "/root/work/runs/r-42/pid" in pid_check[2]


async def test_start_exec_no_env_skips_env_file_write_and_arg(
    fast_sleep: list[float],
):

    sandbox = _FakeSandbox(
        exec_results=[
            ExecResult(0, "", ""),  # spawn
            ExecResult(0, "12345", ""),  # pid
        ],
    )

    await start_exec(
        host=sandbox,  # type: ignore[arg-type]
        run_id="r-1",
        cmd=["true"],
        cwd="/tmp",
    )

    assert len(sandbox.exec_log) == 2  # spawn + pid; no env write
    spawn = sandbox.exec_log[0]
    assert "/root/druks-sandbox exec-start" in spawn[2]
    # ``--env-file`` flag entirely omitted, not just empty-valued.
    assert "--env-file" not in spawn[2]


async def test_start_exec_raises_when_env_file_write_fails(
    fast_sleep: list[float],
):

    sandbox = _FakeSandbox(
        exec_results=[
            ExecResult(1, "", "permission denied"),  # env write fails
        ],
    )

    with pytest.raises(ExecFailed, match="env file"):
        await start_exec(
            host=sandbox,  # type: ignore[arg-type]
            run_id="r-1",
            cmd=["true"],
            cwd="/tmp",
            env={"K": "v"},
        )


async def test_start_exec_raises_when_pid_file_never_appears(
    fast_sleep: list[float],
):

    sandbox = _FakeSandbox(
        exec_results=[
            ExecResult(0, "", ""),  # mkdir
            ExecResult(0, "", ""),  # spawn
            # pid check always returns empty — the sticky-last fallback
            # in _FakeSandbox keeps returning this.
            ExecResult(0, "", ""),
        ],
    )

    # Compress the timeout so the test finishes quickly.
    with (
        patch("druks.sandbox.runner.SPAWN_TIMEOUT_SECONDS", 0.5),
        pytest.raises(ExecFailed, match="did not write"),
    ):
        await start_exec(
            host=sandbox,  # type: ignore[arg-type]
            run_id="r-1",
            cmd=["true"],
            cwd="/tmp",
        )


async def test_render_env_line_single_quote_escaping():

    line = runner._render_env_line("KEY", "val 'with' apostrophes")

    # Shell-source-safe: close quote, escape, reopen quote.
    assert line == "KEY='val '\\''with'\\'' apostrophes'"


async def test_tail_yields_initial_bytes_and_terminates_on_exit_code(
    fast_sleep: list[float],
):
    vm = _FakeVM()
    vm.write("/work/runs/r-1/stdout.jsonl", b"hello world\n")
    vm.write("/work/runs/r-1/exit_code", b"0")
    sandbox = _FakeSandbox(vm=vm)
    run = Exec(host=sandbox, run_id="r-1", run_dir="/work/runs/r-1")  # type: ignore[arg-type]

    chunks = [chunk async for chunk in run.tail("stdout")]

    assert b"".join(chunks) == b"hello world\n"


async def test_tail_streams_growing_file_until_done(
    fast_sleep: list[float],
):

    vm = _FakeVM()
    sandbox = _FakeSandbox(vm=vm)
    run = Exec(host=sandbox, run_id="r-1", run_dir="/work/runs/r-1")  # type: ignore[arg-type]

    # Inject writes between polls by patching asyncio.sleep to advance
    # the VM state. Each "sleep" represents a tick of the poll loop.
    growth = iter(
        [
            # Tick 0: caller starts polling; we write some bytes.
            (b'{"type":"start"}\n', None),
            # Tick 1: more bytes.
            (b'{"type":"progress"}\n', None),
            # Tick 2: final bytes + exit_code.
            (b'{"type":"done"}\n', b"0"),
        ]
    )

    async def fake_sleep(seconds: float) -> None:
        try:
            stdout_chunk, exit_code = next(growth)
        except StopIteration:
            return
        if stdout_chunk:
            vm.append("/work/runs/r-1/stdout.jsonl", stdout_chunk)
        if exit_code is not None:
            vm.write("/work/runs/r-1/exit_code", exit_code)

    with patch("druks.sandbox.runner.asyncio.sleep", fake_sleep):
        chunks = [chunk async for chunk in run.tail("stdout")]

    payload = b"".join(chunks)
    assert b'{"type":"start"}' in payload
    assert b'{"type":"progress"}' in payload
    assert b'{"type":"done"}' in payload


async def test_tail_resumes_from_offset(fast_sleep: list[float]):

    vm = _FakeVM()
    vm.write("/work/runs/r-1/stdout.jsonl", b"AAAAABBBBB")
    vm.write("/work/runs/r-1/exit_code", b"0")
    sandbox = _FakeSandbox(vm=vm)
    run = Exec(host=sandbox, run_id="r-1", run_dir="/work/runs/r-1")  # type: ignore[arg-type]

    chunks = [chunk async for chunk in run.tail("stdout", from_offset=5)]

    assert b"".join(chunks) == b"BBBBB"


async def test_tail_drains_bytes_written_after_exit_code(
    fast_sleep: list[float],
):

    vm = _FakeVM()
    sandbox = _FakeSandbox(vm=vm)
    run = Exec(host=sandbox, run_id="r-1", run_dir="/work/runs/r-1")  # type: ignore[arg-type]

    poll_count = 0

    async def fake_sleep(seconds: float) -> None:
        nonlocal poll_count
        poll_count += 1
        if poll_count == 1:
            # Append + write exit_code AFTER the size check completed
            # in this tick but BEFORE the next iteration.
            vm.append("/work/runs/r-1/stdout.jsonl", b"first\n")
            vm.write("/work/runs/r-1/exit_code", b"0")
            # Simulate the race: another append squeaks in.
            vm.append("/work/runs/r-1/stdout.jsonl", b"squeezed-in\n")

    with patch("druks.sandbox.runner.asyncio.sleep", fake_sleep):
        chunks = [chunk async for chunk in run.tail("stdout")]

    assert b"first\n" in b"".join(chunks)
    assert b"squeezed-in\n" in b"".join(chunks)


async def test_tail_reconnects_after_transient_failure(
    fast_sleep: list[float],
):

    vm = _FakeVM()
    vm.write("/work/runs/r-1/stdout.jsonl", b"surviving bytes\n")
    vm.write("/work/runs/r-1/exit_code", b"0")
    sandbox = _FakeSandbox(vm=vm)
    sandbox.conn.fail_remaining = 1  # first start_sftp_client raises
    run = Exec(host=sandbox, run_id="r-1", run_dir="/work/runs/r-1")  # type: ignore[arg-type]

    chunks = [chunk async for chunk in run.tail("stdout")]

    assert b"".join(chunks) == b"surviving bytes\n"
    # The runner forced a connection bounce after the failure.
    assert sandbox.aclose_calls >= 1
    # At least one backoff slept (in addition to poll sleeps).
    assert any(s >= runner.RECONNECT_BACKOFF_BASE_SECONDS for s in fast_sleep)


async def test_tail_raises_sandbox_unreachable_after_budget_exceeded(
    fast_sleep: list[float],
):

    vm = _FakeVM()
    sandbox = _FakeSandbox(vm=vm)
    sandbox.conn.fail_remaining = 9999  # never recovers
    run = Exec(host=sandbox, run_id="r-1", run_dir="/work/runs/r-1")  # type: ignore[arg-type]

    # Compress the budget so the test doesn't actually wait 5 minutes
    # of monotonic time.
    fake_now_seq = iter([0.0, 0.5, 1.0, 1.5, 999.0])

    def fake_now() -> float:
        return next(fake_now_seq, 1000.0)

    with (
        patch("druks.sandbox.runner.time.monotonic", fake_now),
        pytest.raises(SandboxUnreachable, match="reconnect"),
    ):
        async for _ in run.tail("stdout"):
            pass


async def test_wait_returns_exit_code_when_already_done(fast_sleep: list[float]):
    vm = _FakeVM()
    vm.write("/work/runs/r-1/exit_code", b"0")
    sandbox = _FakeSandbox(vm=vm)
    run = Exec(host=sandbox, run_id="r-1", run_dir="/work/runs/r-1")  # type: ignore[arg-type]

    assert await run.wait() == 0


async def test_wait_polls_until_exit_code_appears(fast_sleep: list[float]):
    vm = _FakeVM()
    sandbox = _FakeSandbox(vm=vm)
    run = Exec(host=sandbox, run_id="r-1", run_dir="/work/runs/r-1")  # type: ignore[arg-type]

    poll_count = 0

    async def fake_sleep(seconds: float) -> None:
        nonlocal poll_count
        poll_count += 1
        if poll_count >= 3:
            vm.write("/work/runs/r-1/exit_code", b"42")

    with patch("druks.sandbox.runner.asyncio.sleep", fake_sleep):
        exit_code = await run.wait()

    assert exit_code == 42
    assert poll_count >= 3


async def test_kill_invokes_druks_sandbox_exec_kill_verb(fast_sleep: list[float]):
    sandbox = _FakeSandbox()
    run = Exec(host=sandbox, run_id="r-1", run_dir="/work/runs/r-1")  # type: ignore[arg-type]

    await run.kill()

    assert sandbox.exec_log == [
        ["/root/druks-sandbox", "exec-kill", "--run-id", "r-1"],
    ]


async def test_attach_returns_run_with_correct_paths():
    sandbox = _FakeSandbox()

    run = await attach(
        host=sandbox,  # type: ignore[arg-type]
        run_id="r-7",
    )

    assert run.run_id == "r-7"
    assert run.run_dir == "/root/work/runs/r-7"
    assert run.stdout_path == "/root/work/runs/r-7/stdout.jsonl"
    assert run.stderr_path == "/root/work/runs/r-7/stderr.log"
    assert run.exit_code_path == "/root/work/runs/r-7/exit_code"
    assert run.pid_path == "/root/work/runs/r-7/pid"


def test_backoff_is_monotonic_and_capped():

    deltas = [runner._backoff(i) for i in range(1, 10)]

    assert deltas[0] == 1.0
    assert deltas[1] == 2.0
    assert deltas[2] == 4.0
    # Monotonic non-decreasing.
    # ``strict=False`` is intentional — ``deltas[1:]`` is one shorter,
    # we want to pair element i with i+1 and stop at the last full pair.
    assert all(a <= b for a, b in zip(deltas, deltas[1:], strict=False))
    # Never exceeds the cap.
    assert max(deltas) <= runner.RECONNECT_BACKOFF_MAX_SECONDS


async def test_completion_reports_running_with_stdout_size():

    vm = _FakeVM()
    vm.write("/work/runs/r-1/stdout.jsonl", b"hello world")
    sandbox = _FakeSandbox(vm=vm)
    run = Exec(host=sandbox, run_id="r-1", run_dir="/work/runs/r-1")  # type: ignore[arg-type]

    result = await run.completion()

    assert result.done is False
    assert result.exit_code is None
    assert result.stdout_bytes == 11


async def test_completion_reports_done_with_exit_code():

    vm = _FakeVM()
    vm.write("/work/runs/r-1/stdout.jsonl", b"out")
    vm.write("/work/runs/r-1/exit_code", b"0")
    sandbox = _FakeSandbox(vm=vm)
    run = Exec(host=sandbox, run_id="r-1", run_dir="/work/runs/r-1")  # type: ignore[arg-type]

    result = await run.completion()

    assert result.done is True
    assert result.exit_code == 0
    assert result.stdout_bytes == 3


async def test_completion_raises_sandbox_unreachable_on_ssh_error():

    sandbox = _FakeSandbox(vm=_FakeVM())
    sandbox.conn.fail_remaining = 1
    run = Exec(host=sandbox, run_id="r-1", run_dir="/work/runs/r-1")  # type: ignore[arg-type]

    with pytest.raises(SandboxUnreachable, match="completion"):
        await run.completion()
    assert sandbox.aclose_calls == 1

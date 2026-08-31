import asyncio
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Self
from unittest.mock import AsyncMock

import pytest
from drukbox_sdk import SandboxHost as SandboxHostRecord
from druks.sandbox.exceptions import SandboxError
from druks.sandbox.host import ExecResult, Host, _stream_local_tar_into


@dataclass
class _FakeCompletedProcess:
    exit_status: int | None = 0
    # Set (with exit_status=None) to model a signal death, mirroring asyncssh.
    exit_signal: tuple[str, bool, str, str] | None = None
    stdout: str = ""
    stderr: str = ""

    @property
    def returncode(self) -> int | None:
        # Mirrors asyncssh: the exit status, else the negative signal number, else None.
        if self.exit_status is not None:
            return self.exit_status
        if self.exit_signal is not None:
            return -9  # SIGKILL — the OOM case
        return None


class _FakeSFTP:
    def __init__(self) -> None:
        self.puts: list[tuple[str, str]] = []
        self.gets: list[tuple[str, str]] = []

    async def put(self, local: str, remote: str) -> None:
        self.puts.append((local, remote))

    async def get(self, remote: str, local: str) -> None:
        self.gets.append((remote, local))

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None


class _FakeConnection:
    def __init__(self) -> None:
        self.sftp = _FakeSFTP()
        self.run_calls: list[tuple[str, dict[str, Any]]] = []
        self.run_result = _FakeCompletedProcess()
        self.closed = False
        self.wait_closed_called = False

    def start_sftp_client(self) -> _FakeSFTP:
        # Note: asyncssh's real ``start_sftp_client`` is async; here it
        # returns the async-context-manager directly. Tests await it via
        # ``async with`` so the return-value semantics line up.
        return self.sftp

    async def run(
        self,
        command: str,
        *,
        check: bool,
        timeout: float,
    ) -> _FakeCompletedProcess:
        self.run_calls.append((command, {"check": check, "timeout": timeout}))
        return self.run_result

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        self.wait_closed_called = True


@pytest.fixture
def fake_record() -> SandboxHostRecord:
    return SandboxHostRecord(
        id="host-abc",
        name="abc",
        status="active",
        provider="exe.dev",
        image="ghcr.io/.../sandbox:test",
        external_ssh_host="abc.public.exe.xyz",
        external_ssh_port=22,
        ssh_username="root",
        internal_ssh_host="abc.tail-scale.ts.net",
        known_hosts="ssh-ed25519 AAAA test-key\n",
        tailscale_device_id="dev-123",
        private_key=None,
        last_error="",
        created_at="2026-05-28T12:00:00+00:00",
        updated_at="2026-05-28T12:00:00+00:00",
        activated_at="2026-05-28T12:00:02+00:00",
        expires_at=None,
        instance_type=None,
        disk_gb=None,
    )


@pytest.fixture
def patched_asyncssh(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[AsyncMock, _FakeConnection]:
    fake_conn = _FakeConnection()
    connect_mock = AsyncMock(return_value=fake_conn)
    monkeypatch.setattr("druks.sandbox.host.asyncssh.connect", connect_mock)
    # Also stub ``import_known_hosts`` so we don't need a real
    # OpenSSH-format string — return a sentinel and assert on it.
    monkeypatch.setattr(
        "druks.sandbox.host.asyncssh.import_known_hosts",
        lambda raw: ("known-hosts-sentinel", raw),
    )
    return connect_mock, fake_conn


def test_expires_at_parses_the_record_lease(fake_record: SandboxHostRecord):
    """expires_at reads the record's ISO lease as an aware datetime, None when absent."""
    leased = Host(record=replace(fake_record, expires_at="2026-05-28T12:00:00+00:00"))
    assert leased.expires_at == datetime(2026, 5, 28, 12, 0, tzinfo=UTC)
    assert Host(record=fake_record).expires_at is None


async def test_connect_is_lazy(
    fake_record: SandboxHostRecord,
    patched_asyncssh: tuple[AsyncMock, _FakeConnection],
):
    connect_mock, _ = patched_asyncssh
    Host(record=fake_record)
    connect_mock.assert_not_called()


async def test_first_call_opens_connection_with_record_details(
    fake_record: SandboxHostRecord,
    patched_asyncssh: tuple[AsyncMock, _FakeConnection],
):
    connect_mock, _ = patched_asyncssh
    sandbox = Host(record=replace(fake_record, ssh_username="druks"))

    await sandbox.exec(["true"])

    connect_mock.assert_awaited_once()
    assert connect_mock.await_args is not None
    kwargs = connect_mock.await_args.kwargs
    assert kwargs["host"] == "abc.tail-scale.ts.net"
    assert kwargs["port"] == 22
    assert kwargs["username"] == "druks"
    assert "client_keys" not in kwargs
    # ``known_hosts`` was sourced from the record via the patched
    # ``import_known_hosts`` shim, not passed raw.
    assert kwargs["known_hosts"] == (
        "known-hosts-sentinel",
        "ssh-ed25519 AAAA test-key\n",
    )


async def test_connection_is_reused_across_calls(
    fake_record: SandboxHostRecord,
    patched_asyncssh: tuple[AsyncMock, _FakeConnection],
    tmp_path: Path,
):
    connect_mock, _ = patched_asyncssh
    sandbox = Host(record=fake_record)

    local = tmp_path / "payload"
    local.write_text("(dummy)")

    await sandbox.exec(["echo", "one"])
    await sandbox.exec(["echo", "two"])
    await sandbox.upload_file(local=local, remote="/tmp/x")

    connect_mock.assert_awaited_once()


async def test_concurrent_first_callers_do_not_open_two_connections(
    fake_record: SandboxHostRecord,
    patched_asyncssh: tuple[AsyncMock, _FakeConnection],
):
    connect_mock, _ = patched_asyncssh
    sandbox = Host(record=fake_record)

    await asyncio.gather(
        sandbox.exec(["echo", "1"]),
        sandbox.exec(["echo", "2"]),
        sandbox.exec(["echo", "3"]),
    )

    connect_mock.assert_awaited_once()


async def test_aclose_closes_and_is_idempotent(
    fake_record: SandboxHostRecord,
    patched_asyncssh: tuple[AsyncMock, _FakeConnection],
):
    connect_mock, fake_conn = patched_asyncssh
    sandbox = Host(record=fake_record)

    # Before first use: nothing to close, nothing dialed.
    await sandbox.aclose()
    connect_mock.assert_not_called()

    await sandbox.exec(["true"])
    await sandbox.aclose()

    assert fake_conn.closed is True
    assert fake_conn.wait_closed_called is True

    # Second close: no error, no double wait.
    fake_conn.wait_closed_called = False
    await sandbox.aclose()
    assert fake_conn.wait_closed_called is False


async def test_async_context_manager_closes_on_exception(
    fake_record: SandboxHostRecord,
    patched_asyncssh: tuple[AsyncMock, _FakeConnection],
):
    _, fake_conn = patched_asyncssh

    with pytest.raises(RuntimeError):
        async with Host(
            record=fake_record,
        ) as sandbox:
            await sandbox.exec(["true"])
            raise RuntimeError("body fails")

    assert fake_conn.closed is True


async def test_upload_pushes_via_sftp(
    fake_record: SandboxHostRecord,
    patched_asyncssh: tuple[AsyncMock, _FakeConnection],
    tmp_path: Path,
):
    _, fake_conn = patched_asyncssh
    sandbox = Host(record=fake_record)

    local = tmp_path / "credentials.json"
    local.write_text('{"token": "..."}')
    await sandbox.upload_file(local=local, remote="/creds/anthropic.json")

    assert fake_conn.sftp.puts == [(str(local), "/creds/anthropic.json")]


async def test_download_fetches_via_sftp_and_creates_parent_dir(
    fake_record: SandboxHostRecord,
    patched_asyncssh: tuple[AsyncMock, _FakeConnection],
    tmp_path: Path,
):
    _, fake_conn = patched_asyncssh
    sandbox = Host(record=fake_record)

    local = tmp_path / "nested" / "subdir" / "out.jsonl"
    await sandbox.download(remote="/work/runs/42/stdout.jsonl", local=local)

    assert fake_conn.sftp.gets == [("/work/runs/42/stdout.jsonl", str(local))]
    assert local.parent.exists()


async def test_exec_oneshot_shell_quotes_argv(
    fake_record: SandboxHostRecord,
    patched_asyncssh: tuple[AsyncMock, _FakeConnection],
):
    _, fake_conn = patched_asyncssh
    sandbox = Host(record=fake_record)

    await sandbox.exec(["git", "commit", "-m", "hello world; rm -rf /"])

    sent, _ = fake_conn.run_calls[-1]
    # Each argv element shell-quoted independently — no expansion of the
    # semicolon, no globbing on the slash.
    assert sent == "git commit -m 'hello world; rm -rf /'"


async def test_exec_oneshot_returns_typed_result_with_ok_helper(
    fake_record: SandboxHostRecord,
    patched_asyncssh: tuple[AsyncMock, _FakeConnection],
):
    _, fake_conn = patched_asyncssh
    fake_conn.run_result = _FakeCompletedProcess(
        exit_status=0,
        stdout="abc123\n",
        stderr="",
    )
    sandbox = Host(record=fake_record)

    result = await sandbox.exec(["git", "rev-parse", "HEAD"])

    assert isinstance(result, ExecResult)
    assert result.exit_code == 0
    assert result.stdout == "abc123\n"
    assert result.ok is True


async def test_exec_oneshot_nonzero_exit_returns_not_raises(
    fake_record: SandboxHostRecord,
    patched_asyncssh: tuple[AsyncMock, _FakeConnection],
):
    _, fake_conn = patched_asyncssh
    fake_conn.run_result = _FakeCompletedProcess(
        exit_status=2,
        stdout="",
        stderr="rg: backend/missing: No such file or directory\n",
    )
    sandbox = Host(record=fake_record)

    result = await sandbox.exec(["rg", "needle", "backend/missing"])

    assert result.exit_code == 2
    assert result.ok is False
    assert "No such file" in result.stderr


async def test_exec_signal_killed_command_is_not_ok(
    fake_record: SandboxHostRecord,
    patched_asyncssh: tuple[AsyncMock, _FakeConnection],
):
    # A command killed by a signal (an OOM'd git clone → SIGKILL) has no exit status;
    # it must read as failure, not be coerced to a successful 0.
    _, fake_conn = patched_asyncssh
    fake_conn.run_result = _FakeCompletedProcess(
        exit_status=None,
        exit_signal=("KILL", True, "", ""),
        stdout="",
        stderr="",
    )
    sandbox = Host(record=fake_record)

    result = await sandbox.exec(["sh", "-c", "git clone https://example/big"])

    assert result.exit_code != 0
    assert result.ok is False


async def test_exec_closed_channel_without_status_is_not_ok(
    fake_record: SandboxHostRecord,
    patched_asyncssh: tuple[AsyncMock, _FakeConnection],
):
    # Channel closed with neither exit status nor signal (asyncssh returncode is None):
    # fall back to the -1 sentinel rather than a success.
    _, fake_conn = patched_asyncssh
    fake_conn.run_result = _FakeCompletedProcess(exit_status=None)
    sandbox = Host(record=fake_record)

    result = await sandbox.exec(["true"])

    assert result.exit_code == -1
    assert result.ok is False


# Tar streaming — exercises the local-tar half of upload_dir against a real
# ``tar -xf -`` subprocess. The SSH half is covered by the integration
# suite; here we only need to know we build the right tar.


class _SubprocessTarSink:
    def __init__(self, proc: asyncio.subprocess.Process) -> None:
        assert proc.stdin is not None
        self._proc = proc
        self._stdin = proc.stdin

    def write(self, data: bytes) -> None:
        self._stdin.write(data)

    async def drain(self) -> None:
        await self._stdin.drain()

    def write_eof(self) -> None:
        self._stdin.write_eof()


async def _roundtrip_via_tar(
    src: Path,
    dst: Path,
    excludes: tuple[str, ...] = (),
) -> None:
    dst.mkdir(parents=True, exist_ok=True)
    untar = await asyncio.create_subprocess_exec(
        "tar",
        "-xmf",
        "-",
        "-C",
        str(dst),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    sink = _SubprocessTarSink(untar)
    try:
        await _stream_local_tar_into(local=src, excludes=excludes, writer=sink)
    finally:
        rc = await untar.wait()
    if rc != 0:
        stderr = (await untar.stderr.read()).decode() if untar.stderr else ""
        raise AssertionError(f"untar exited {rc}: {stderr}")


async def test_upload_dir_tar_roundtrip_mirrors_tree(tmp_path: Path):
    src = tmp_path / "src"
    (src / "deep" / "nest").mkdir(parents=True)
    (src / "a.txt").write_text("alpha")
    (src / "deep" / "b.txt").write_text("beta")
    (src / "deep" / "nest" / "c.bin").write_bytes(b"\x00\x01\x02")

    dst = tmp_path / "dst"
    await _roundtrip_via_tar(src, dst)

    assert (dst / "a.txt").read_text() == "alpha"
    assert (dst / "deep" / "b.txt").read_text() == "beta"
    assert (dst / "deep" / "nest" / "c.bin").read_bytes() == b"\x00\x01\x02"


async def test_upload_dir_tar_honours_excludes(tmp_path: Path):
    src = tmp_path / "src"
    (src / "plugin" / ".in_use").mkdir(parents=True)
    (src / "plugin" / ".in_use" / "12345").write_text("marker")
    (src / "plugin" / ".in_use" / "67890").write_text("marker")
    (src / "plugin" / ".git").mkdir()
    (src / "plugin" / ".git" / "HEAD").write_text("ref: refs/heads/main")
    (src / "plugin" / "node_modules").mkdir()
    (src / "plugin" / "node_modules" / "lodash.js").write_text("noise")
    (src / "plugin" / "skill.md").write_text("real content")

    dst = tmp_path / "dst"
    await _roundtrip_via_tar(
        src,
        dst,
        excludes=(".in_use", ".git", "node_modules"),
    )

    assert (dst / "plugin" / "skill.md").read_text() == "real content"
    assert not (dst / "plugin" / ".in_use").exists()
    assert not (dst / "plugin" / ".git").exists()
    assert not (dst / "plugin" / "node_modules").exists()


async def test_write_secret_raises_when_the_remote_write_fails(fake_record):
    # push() now writes the harness OAuth credential through write_secret, so a
    # failed remote write must fail the run, not start the agent unauthenticated.
    sandbox = Host(record=fake_record)

    async def failing_exec(cmd, *, timeout=30.0):
        return ExecResult(1, "", "permission denied")

    sandbox.exec = failing_exec  # type: ignore[method-assign]

    with pytest.raises(SandboxError) as error:
        await sandbox.write_secret(remote="/home/exedev/.codex/auth.json", secret="s3cret-token")

    # The secret must never ride the surfaced error.
    assert "s3cret-token" not in str(error.value)
    assert "permission denied" in str(error.value)

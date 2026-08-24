import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from druks.durable.enums import AgentCallStatus
from druks.harnesses.base import Harness
from druks.harnesses.claude import ClaudeHarness
from druks.harnesses.codex import CodexHarness
from druks.harnesses.exceptions import (
    HarnessAuthError,
    HarnessError,
    HarnessFirstByteTimeoutError,
    HarnessOverloadedError,
    HarnessRateLimitError,
    HarnessSandboxError,
    HarnessTimeoutError,
    HarnessUsageLimitError,
    Retry,
)
from druks.sandbox.datastructures import (
    AgentInvocation,
    AgentResult,
    Credentials,
    HarnessRunResult,
)
from druks.sandbox.exceptions import SandboxUnreachable
from druks.sandbox.host import Sandbox


@dataclass
class _FakeRun:
    run_id: str = "rid"
    run_dir: str = "/work/runs/rid"
    host: Any = field(default_factory=lambda: SimpleNamespace(id="host-abc"))
    stdout_chunks: list[bytes] = field(default_factory=list)
    stderr_chunks: list[bytes] = field(default_factory=list)
    exit_code: int = 0
    wait_delay: float = 0.0
    kill_calls: int = 0

    async def tail(self, stream: str, *, from_offset: int = 0) -> AsyncIterator[bytes]:
        del from_offset
        chunks = self.stdout_chunks if stream == "stdout" else self.stderr_chunks
        for chunk in chunks:
            yield chunk
            await asyncio.sleep(0)

    async def wait(self) -> int:
        if self.wait_delay:
            await asyncio.sleep(self.wait_delay)
        return self.exit_code

    async def kill(self) -> None:
        self.kill_calls += 1


@dataclass
class _LifecycleCall:
    kwargs: dict[str, Any]


def _fake_sandbox(run: _FakeRun) -> SimpleNamespace:
    """Build a Sandbox-stub for the flat exec path.

    ``Sandbox._exec`` / ``Sandbox.run_prompt`` are invoked unbound with this
    stub as ``self`` — they only touch ``_start_instruction`` /
    ``_download_artifacts`` / ``ssh_username``. The ``calls`` list captures
    every start's kwargs; ``downloads`` the artifact pulls.
    """
    calls: list[_LifecycleCall] = []
    downloads: list[dict[str, Any]] = []

    async def _start_instruction(**kwargs: Any) -> _FakeRun:
        calls.append(_LifecycleCall(kwargs=kwargs))
        return run

    async def _download_artifacts(
        run_: Any,
        local_dir: Path,
        *,
        extra_filenames: tuple[str, ...] = (),
    ) -> None:
        downloads.append({"local_dir": local_dir, "extra_filenames": extra_filenames})

    fake = SimpleNamespace(id="host-abc", ssh_username="root")
    fake._start_instruction = _start_instruction
    fake._download_artifacts = _download_artifacts
    fake.calls = calls
    fake.downloads = downloads
    return fake


@pytest.fixture
def ctx(tmp_path: Path) -> SimpleNamespace:
    artifact_dir = tmp_path / "artifacts"
    artifact_dir.mkdir(parents=True)
    return SimpleNamespace(artifact_dir=artifact_dir)


_DEFAULT_RUN_ID = "claude-implement-42-1"


def _inv(args: tuple[str, ...], **overrides: Any) -> AgentInvocation:
    fields: dict[str, Any] = {
        "name": "claude",
        "args": args,
        "stdin": b"prompt-bytes",
        "credentials": Credentials(github_token="gho_x"),
    }
    fields.update(overrides)
    return AgentInvocation(**fields)


async def test_returns_harness_run_result_with_stdout_stderr(
    ctx: SimpleNamespace,
):
    run = _FakeRun(
        stdout_chunks=[b"line one\n", b"line two\n"],
        stderr_chunks=[b"warn: x\n"],
        exit_code=0,
    )
    sandbox = _fake_sandbox(run)

    result = await Sandbox._exec(
        sandbox,
        _inv(("claude", "--print", "hi")),
        run_id=_DEFAULT_RUN_ID,
        artifact_dir=ctx.artifact_dir,
        timeout=60,
    )

    assert result.returncode == 0
    assert result.stdout == b"line one\nline two\n"
    assert result.stderr == b"warn: x\n"


async def test_writes_log_files_in_dashboard_convention(
    ctx: SimpleNamespace,
):
    run = _FakeRun(stdout_chunks=[b"hello\n"], stderr_chunks=[b"oops\n"])
    sandbox = _fake_sandbox(run)

    await Sandbox._exec(
        sandbox,
        _inv(("claude",)),
        run_id=_DEFAULT_RUN_ID,
        artifact_dir=ctx.artifact_dir,
        timeout=60,
    )

    call_dir = ctx.artifact_dir / _DEFAULT_RUN_ID
    stdout_log = call_dir / "stdout.jsonl"
    stderr_log = call_dir / "stderr.log"
    metadata = call_dir / "metadata.json"
    assert stdout_log.read_bytes() == b"hello\n"
    assert stderr_log.read_bytes() == b"oops\n"
    # Metadata has both the start-time record (written before exec)
    # and the final record (overwritten with elapsed + exit_code).
    payload = json.loads(metadata.read_text())
    assert payload["name"] == "claude"
    assert payload["exit_code"] == 0
    assert payload["timed_out"] is False
    assert payload["first_byte_killed"] is False
    assert "elapsed_seconds" in payload


async def test_forwards_invocation_to_lifecycle(
    ctx: SimpleNamespace,
):
    run = _FakeRun(stdout_chunks=[b""])
    sandbox = _fake_sandbox(run)

    await Sandbox._exec(
        sandbox,
        _inv(
            ("codex", "exec", "--print"),
            name="codex",
            env={"FOO": "bar"},
            cwd="/work/checkout",
        ),
        run_id="codex-evaluate_implementation-42-1",
        artifact_dir=ctx.artifact_dir,
        timeout=900,
    )

    assert len(sandbox.calls) == 1
    kwargs = sandbox.calls[0].kwargs
    assert kwargs["cmd"] == ["codex", "exec", "--print"]
    assert kwargs["extra_env"] == {"FOO": "bar"}
    assert kwargs["cwd"] == "/work/checkout"
    assert kwargs["stdin_data"] == b"prompt-bytes"
    # run_id is supplied by the caller (run_prompt mints it as name +
    # operation + work_item_id + revision so multiple operations on the
    # same work item don't collide on /work/runs/<run_id>) and forwarded
    # to the lifecycle verbatim.
    assert kwargs["run_id"] == "codex-evaluate_implementation-42-1"
    # The CLI-specific artifacts ride along to the download.
    assert sandbox.downloads == [
        {
            "local_dir": ctx.artifact_dir / "codex-evaluate_implementation-42-1",
            "extra_filenames": (),
        }
    ]


async def test_overall_timeout_raises_harness_timeout_error(
    ctx: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
):
    run = _FakeRun(stdout_chunks=[b"some output\n"], wait_delay=10.0)
    sandbox = _fake_sandbox(run)

    # Compress the actual sleep so the test finishes in <1s. Patch
    # asyncio.wait_for to enforce a tiny budget.
    real_wait_for = asyncio.wait_for

    async def fast_wait_for(coro: Any, timeout: float) -> Any:
        return await real_wait_for(coro, timeout=min(timeout, 0.05))

    monkeypatch.setattr("druks.sandbox.host.asyncio.wait_for", fast_wait_for)

    with pytest.raises(HarnessTimeoutError, match="timed out"):
        await Sandbox._exec(
            sandbox,
            _inv(("claude",)),
            run_id=_DEFAULT_RUN_ID,
            artifact_dir=ctx.artifact_dir,
            timeout=60,
        )

    # We tried to clean up by killing the run before bailing.
    assert run.kill_calls >= 1


async def test_first_byte_kill_raises_typed_error(
    ctx: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
):
    run = _FakeRun(stdout_chunks=[], stderr_chunks=[], wait_delay=10.0)
    sandbox = _fake_sandbox(run)

    # Capture the originals BEFORE patching — otherwise patching
    # ``asyncio.sleep`` / ``asyncio.wait_for`` aliases the singleton
    # ``asyncio`` module's attributes and the fakes recurse into
    # themselves.
    real_sleep = asyncio.sleep
    real_wait_for = asyncio.wait_for

    async def fast_sleep(seconds: float) -> None:
        # Don't compress the 0-arg yield (used for scheduling).
        await real_sleep(0 if seconds == 0 else min(seconds, 0.02))

    async def fast_wait_for(coro: Any, timeout: float) -> Any:
        # Keep the relative ordering — overall wait_for needs a budget
        # *bigger* than the first-byte killer's window so the killer
        # fires first.
        return await real_wait_for(coro, timeout=0.2)

    monkeypatch.setattr("druks.sandbox.host.asyncio.sleep", fast_sleep)
    monkeypatch.setattr("druks.sandbox.host.asyncio.wait_for", fast_wait_for)

    with pytest.raises(HarnessFirstByteTimeoutError, match="no output"):
        await Sandbox._exec(
            sandbox,
            _inv(("claude",)),
            run_id=_DEFAULT_RUN_ID,
            artifact_dir=ctx.artifact_dir,
            timeout=60,
            first_byte_kill_seconds=1,
        )

    assert run.kill_calls >= 1


async def test_sandbox_unreachable_translates_to_harness_error(
    ctx: SimpleNamespace,
):
    async def boom(**_kwargs: Any) -> Any:
        raise SandboxUnreachable("host vanished mid-run")

    downloads: list[Any] = []

    async def _download_artifacts(*args: Any, **kwargs: Any) -> None:
        downloads.append((args, kwargs))

    sandbox = SimpleNamespace(id="host-abc", ssh_username="root")
    sandbox._start_instruction = boom
    sandbox._download_artifacts = _download_artifacts

    with pytest.raises(HarnessSandboxError, match="sandbox failure") as excinfo:
        await Sandbox._exec(
            sandbox,
            _inv(("claude",)),
            run_id=_DEFAULT_RUN_ID,
            artifact_dir=ctx.artifact_dir,
            timeout=60,
        )

    assert excinfo.value.retry is Retry.TRANSIENT
    # Start never produced a run → nothing to download from.
    assert downloads == []


async def _harness_stub(model):
    return ClaudeHarness


async def test_run_prompt_builds_executes_and_parses(
    ctx: SimpleNamespace,
):
    """The harness-sandbox seam: the sandbox orchestrates build → exec → parse;
    the harness never sees the live sandbox, only ``ssh_username``."""
    run = _FakeRun(stdout_chunks=[b"streamed\n"], exit_code=0)
    sandbox = _fake_sandbox(run)
    # run_prompt delegates to self._exec — bind the real one onto the stub.
    sandbox._exec = lambda *args, **kwargs: Sandbox._exec(sandbox, *args, **kwargs)
    seen: dict[str, Any] = {}

    class _FakeHarness:
        name = "claude"
        model = "claude-opus-4-8"
        # The real manifest method on the stubbed build/parse seam, so the
        # test exercises what run_prompt actually writes.
        get_manifest = Harness.get_manifest

        @staticmethod
        def mint_run_id(call_id: str | None) -> str:
            return call_id or "minted-id"

        async def build_invocation(self, **kwargs: Any) -> AgentInvocation:
            seen["build"] = kwargs
            return _inv(("claude", "--print"))

        def parse(self, result: Any, *, artifact_dir: Path, run_id: str) -> Any:
            seen["parse"] = {"result": result, "artifact_dir": artifact_dir, "run_id": run_id}
            return {"answer": 42}

    payload = await Sandbox.run_prompt(
        sandbox,
        _FakeHarness(),
        prompt="do the thing",
        schema={"type": "object"},
        artifact_dir=ctx.artifact_dir,
        timeout=60,
        call_id="call-7",
        extra_env={"GITHUB_MCP_TOKEN": "ghs_x"},
    )

    assert payload == {"answer": 42}
    # The harness planned from values, not the live sandbox.
    assert seen["build"]["ssh_username"] == "root"
    assert seen["build"]["extra_env"] == {"GITHUB_MCP_TOKEN": "ghs_x"}
    # The prompt was persisted to the artifact dir before exec.
    assert (ctx.artifact_dir / "call-7" / "prompt.md").read_text() == "do the thing"
    # The capability manifest was written alongside it, keyed to the same call.
    manifest = json.loads((ctx.artifact_dir / "call-7" / "manifest.json").read_text())
    assert manifest["model"] == "claude-opus-4-8"
    assert manifest["harness"] == "claude"
    # parse received the executed result for the same run.
    assert seen["parse"]["run_id"] == "call-7"
    assert seen["parse"]["result"].stdout == b"streamed\n"


def test_check_returncode_surfaces_the_streams_terminal_error():
    """A usage-limit death persisted as a bare 'claude exited with 1.' —
    the reason (and its reset time) was sitting in the last result event."""
    stdout = (
        b'{"type":"assistant","message":{}}\n'
        b'{"type":"result","subtype":"success","is_error":true,'
        b'"result":"You\'ve hit your session limit \xc2\xb7 resets 5:10pm (UTC)"}\n'
    )
    with pytest.raises(HarnessError, match="session limit"):
        ClaudeHarness.check_returncode(HarnessRunResult(returncode=1, stdout=stdout, stderr=b""))


def test_check_returncode_stays_bare_without_a_terminal_event():

    with pytest.raises(HarnessError, match=r"claude exited with 2\.$") as excinfo:
        ClaudeHarness.check_returncode(
            HarnessRunResult(returncode=2, stdout=b"not json\n", stderr=b"")
        )

    assert type(excinfo.value) is HarnessError
    assert excinfo.value.code == ""
    assert excinfo.value.retry is Retry.NEVER


def _claude_result_event(text: str) -> bytes:
    payload = {"type": "result", "subtype": "success", "is_error": True, "result": text}
    return json.dumps(payload).encode() + b"\n"


@pytest.mark.parametrize(
    ("harness", "stdout", "expected", "code"),
    [
        (
            ClaudeHarness,
            _claude_result_event(
                'API Error: 529 {"type":"error","error":'
                '{"type":"overloaded_error","message":"Overloaded"}}'
            ),
            HarnessOverloadedError,
            "overloaded",
        ),
        (
            ClaudeHarness,
            _claude_result_event(
                'API Error: 429 {"type":"error","error":'
                '{"type":"rate_limit_error","message":"Request rate limit exceeded"}}'
            ),
            HarnessRateLimitError,
            "rate_limited",
        ),
        (
            ClaudeHarness,
            _claude_result_event("You've hit your session limit · resets 5:10pm (UTC)"),
            HarnessUsageLimitError,
            "usage_limit",
        ),
        (
            ClaudeHarness,
            _claude_result_event(
                'API Error: 401 {"type":"error","error":'
                '{"type":"authentication_error","message":"OAuth token has expired"}}'
            ),
            HarnessAuthError,
            "auth",
        ),
        (
            ClaudeHarness,
            _claude_result_event(
                "Failed to authenticate: OAuth session expired and could not be refreshed"
            ),
            HarnessAuthError,
            "auth",
        ),
        (
            ClaudeHarness,
            _claude_result_event(
                'API Error: 503 {"type":"error","error":'
                '{"type":"api_error","message":"Internal server error"}}'
            ),
            HarnessOverloadedError,
            "overloaded",
        ),
        (
            CodexHarness,
            b'{"type":"error","message":"stream error: stream disconnected before completion"}\n',
            HarnessOverloadedError,
            "overloaded",
        ),
        (
            CodexHarness,
            b'{"type":"error","message":"You\'ve hit your usage limit."}\n',
            HarnessUsageLimitError,
            "usage_limit",
        ),
    ],
)
def test_check_returncode_classifies_the_terminal_error(
    harness: type[Harness], stdout: bytes, expected: type[HarnessError], code: str
):
    with pytest.raises(expected) as excinfo:
        harness.check_returncode(HarnessRunResult(returncode=1, stdout=stdout, stderr=b""))

    assert type(excinfo.value) is expected
    assert excinfo.value.code == code
    assert str(excinfo.value).startswith(f"{harness.name} exited with 1. ")


def test_agent_result_names_the_agent_in_its_failure():
    result = AgentResult(
        output=None,
        run_id=_DEFAULT_RUN_ID,
        sandbox_host_id="host-abc",
        model="claude-opus-5",
        agent="evaluate_implementation",
        status=AgentCallStatus.FAILED,
        started_at=datetime.now(UTC),
        error=HarnessOverloadedError("claude exited with 1. API Error: 529"),
    )

    assert result.last_error == (
        "evaluate_implementation: HarnessOverloadedError: claude exited with 1. API Error: 529"
    )
    assert result.error.code == "overloaded"


def _patch_harness_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    # run_agent resolves the harness + its settings from the DB; these tests
    # are about the failure boundary, not resolution.

    monkeypatch.setattr("druks.harnesses.registry.get_harness_for_model", _harness_stub)

    async def require(name):
        return SimpleNamespace(effort="high", timeout=60, fast_mode=False)

    monkeypatch.setattr("druks.user_settings.models.HarnessSettings.require", staticmethod(require))


async def test_run_agent_carries_foreign_failures_as_harness_errors(
    ctx: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
):
    """The result's error is always from the taxonomy: a foreign failure is
    wrapped unclassified, keeps its traceback via the chain, and — unlike an
    asyncssh or httpx error — survives DBOS's pickled step record."""
    import pickle

    _patch_harness_resolution(monkeypatch)
    sandbox = SimpleNamespace(id="host-abc", ssh_username="root")
    original = RuntimeError("kaboom")

    async def run_prompt(harness: Any, **_kwargs: Any) -> Any:
        raise original

    sandbox.run_prompt = run_prompt
    result = await Sandbox.run_agent(
        sandbox,
        model="claude-opus-4-7",
        prompt="p",
        schema={"type": "object"},
        agent="evaluate",
        artifact_dir=ctx.artifact_dir,
    )

    assert result.status is AgentCallStatus.FAILED
    assert type(result.error) is HarnessError
    assert result.error.code == ""
    assert result.error.__cause__ is original
    assert result.last_error == "evaluate: HarnessError: RuntimeError: kaboom"
    revived = pickle.loads(pickle.dumps(result.error))
    assert type(revived) is HarnessError and revived.__cause__ is None


async def test_run_agent_carries_a_taxonomy_failure_as_itself(
    ctx: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
):
    _patch_harness_resolution(monkeypatch)
    sandbox = SimpleNamespace(id="host-abc", ssh_username="root")
    timeout = HarnessTimeoutError("claude timed out after 60s.")

    async def run_prompt(harness: Any, **_kwargs: Any) -> Any:
        raise timeout

    sandbox.run_prompt = run_prompt
    result = await Sandbox.run_agent(
        sandbox,
        model="claude-opus-4-7",
        prompt="p",
        schema={"type": "object"},
        agent="evaluate",
        artifact_dir=ctx.artifact_dir,
    )

    assert result.error is timeout

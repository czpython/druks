from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest
from conftest import make_agent_result
from druks import agents
from druks.durable import AgentCall, WorkflowError


class DummyOutput(agents.AgentOutput):
    ok: bool


DUMMY_AGENT = agents.Agent(
    id="dummy",
    prompt="dummy/agent.md",
    contract=DummyOutput,
    model="claude-haiku-4-5",
)


def test_get_timeout_caps_at_the_sandbox_lease_max(db_session):
    """A resolved timeout over the sandbox-lease max is clamped; a shorter one passes through."""
    from druks.sandbox.constants import MAX_AGENT_TIMEOUT_SECONDS

    over = agents.Agent(
        id="over",
        prompt="dummy/agent.md",
        contract=DummyOutput,
        model="claude-haiku-4-5",
        timeout=MAX_AGENT_TIMEOUT_SECONDS * 2,
    )
    under = agents.Agent(
        id="under",
        prompt="dummy/agent.md",
        contract=DummyOutput,
        model="claude-haiku-4-5",
        timeout=600,
    )

    assert over.get_timeout() == MAX_AGENT_TIMEOUT_SECONDS
    assert under.get_timeout() == 600


@pytest.fixture(autouse=True)
def _seed_run_for_record(db_session):
    # An agent call records an AgentCall, which FKs to its run.
    from conftest import seed_run

    seed_run(db_session, "wf-9")


@pytest.fixture(autouse=True)
def _connected_claude(db_session):
    # A run refuses to dispatch on an unconnected harness; the runtime tests
    # here resolve to claude models, so connect it once.
    from conftest import connect_harness
    from druks.harnesses.claude import ClaudeHarness

    connect_harness(ClaudeHarness, {"claudeAiOauth": {"accessToken": "test-token"}})


def _patch_runtime(monkeypatch, tmp_path, payload):
    """Pin settings/prompt; returns a fake sandbox capturing the run_agent
    call. The agent's model/effort/timeout resolve via the override store
    against the test ``db_session``."""
    settings = MagicMock()
    settings.artifacts_dir = tmp_path
    monkeypatch.setattr(agents, "load_settings", lambda: settings)

    async def _prompt(name, /, *, repo=None, **context):
        return f"PROMPT:{name}:repo={repo}"

    monkeypatch.setattr(agents, "render_prompt", _prompt)
    sandbox = MagicMock()
    # The base Workspace reads host_id off sandbox.id.
    sandbox.id = "host-test"
    sandbox.run_agent = AsyncMock(return_value=make_agent_result(payload, agent="dummy"))
    return sandbox


def _patch_ephemeral(monkeypatch, box):
    # No warm context → the runner's VM comes from ephemeral(); yield our fake box so the
    # base Workspace wraps it and delegates run_agent to it.
    @asynccontextmanager
    async def _fake(self, *, idempotency_key=None, **_kwargs):
        yield box

    monkeypatch.setattr("druks.sandbox.client.Client.ephemeral", _fake)


@pytest.fixture
def current_run():
    # An agent call reads its workflow from current_workflow; set it like the run engine does.
    from druks.workflows import Workflow, current_workflow

    token = current_workflow.set(Workflow())
    yield
    current_workflow.reset(token)


async def test_run_outside_workflow_raises():
    # an agent call needs a workflow context; standalone agent runs aren't supported yet.
    with pytest.raises(WorkflowError, match="inside a workflow"):
        await DUMMY_AGENT(repo="acme/widget")


async def test_run_refuses_unconnected_harness(db_session, tmp_path, monkeypatch, current_run):
    # The precondition fires where the harness is resolved — before any VM work.
    from druks.harnesses.claude import ClaudeHarness
    from druks.harnesses.exceptions import HarnessNotConnectedError

    ClaudeHarness.disconnect()
    sandbox = _patch_runtime(monkeypatch, tmp_path, {"ok": True})
    _patch_ephemeral(monkeypatch, sandbox)

    with pytest.raises(HarnessNotConnectedError, match="connect it in Settings"):
        await DUMMY_AGENT._run(workflow_id="wf-9")

    sandbox.run_agent.assert_not_awaited()


async def test_declaration_drives_run_agent_call(db_session, tmp_path, monkeypatch, current_run):
    # The declaration drives what run_agent sees (model/operation/schema/artifact
    # dir/default timeout); run() returns the validated contract model.
    sandbox = _patch_runtime(monkeypatch, tmp_path, {"ok": True})
    _patch_ephemeral(monkeypatch, sandbox)

    result = await DUMMY_AGENT._run(workflow_id="wf-9", repo="acme/widget")

    assert result == DummyOutput(ok=True)
    kwargs = sandbox.run_agent.await_args.kwargs
    assert kwargs["model"] == "claude-haiku-4-5"
    assert kwargs["agent"] == "dummy"
    assert kwargs["schema"] == DummyOutput.model_json_schema()
    assert kwargs["prompt"] == "PROMPT:dummy/agent.md:repo=acme/widget"
    assert kwargs["artifact_dir"] == tmp_path / "run-wf-9"
    assert kwargs["timeout"] == 1800
    assert kwargs["include_plugins"] is True


async def test_declared_timeout_is_forwarded(db_session, tmp_path, monkeypatch, current_run):
    agent = agents.Agent(
        id="timeout_probe",
        prompt="dummy/agent.md",
        contract=DummyOutput,
        model="claude-opus-4-7",
        timeout=900,
        include_plugins=False,
    )
    sandbox = _patch_runtime(monkeypatch, tmp_path, {"ok": True})
    _patch_ephemeral(monkeypatch, sandbox)

    await agent._run(workflow_id="wf-9")

    kwargs = sandbox.run_agent.await_args.kwargs
    assert kwargs["timeout"] == 900
    assert kwargs["include_plugins"] is False


async def test_runner_comes_from_workflow_workspace_factory(
    db_session, tmp_path, monkeypatch, current_run
):
    """The runner is whatever ``workflow.get_workspace()`` returns — a mode overrides it to
    decide what the agent runs in (a cloned repo, tokens, MCP). The agent runs through it,
    not a bare VM."""
    from druks.workflows import current_workflow

    _patch_runtime(monkeypatch, tmp_path, {"ok": True})
    _patch_ephemeral(monkeypatch, MagicMock())  # the box; the factory below ignores it
    workspace = MagicMock()
    workspace.host_id = "host-test"
    workspace.run_agent = AsyncMock(return_value=make_agent_result({"ok": True}, agent="dummy"))

    async def _get_workspace(sandbox):
        return workspace

    monkeypatch.setattr(current_workflow.get(), "get_workspace", _get_workspace)

    result = await DUMMY_AGENT._run(workflow_id="wf-9")

    assert result == DummyOutput(ok=True)
    workspace.run_agent.assert_awaited_once()
    assert workspace.run_agent.await_args.kwargs["agent"] == "dummy"


async def test_ephemeral_acquisition_keys_idempotency_to_workflow_step(
    db_session, tmp_path, monkeypatch, current_run
):
    """Without a warm context the runtime acquires a throwaway VM, keyed for
    idempotency to ``<workflow_id>:<step>``."""
    sandbox = _patch_runtime(monkeypatch, tmp_path, {"ok": True})
    seen: list[str | None] = []

    @asynccontextmanager
    async def fake_ephemeral(self, *, idempotency_key=None, **_kwargs):
        seen.append(idempotency_key)
        yield sandbox

    monkeypatch.setattr("druks.sandbox.client.Client.ephemeral", fake_ephemeral)

    result = await DUMMY_AGENT._run(workflow_id="wf-9")

    assert result == DummyOutput(ok=True)
    assert seen == ["wf-9:dummy"]


async def test_running_call_visible_then_finished(db_session, tmp_path, monkeypatch, current_run):
    """The AgentCall exists RUNNING on its host while the agent runs, so the live
    transcript has a row to stream onto, and is finished once it returns."""
    sandbox = _patch_runtime(monkeypatch, tmp_path, {"ok": True})
    during: dict[str, object] = {}

    async def _run_agent(*, call_id, **_kwargs):
        row = AgentCall.get(call_id)
        during["status"] = row.status
        during["host"] = row.sandbox_host_id
        return make_agent_result({"ok": True}, agent="dummy")

    sandbox.run_agent = _run_agent
    _patch_ephemeral(monkeypatch, sandbox)

    await DUMMY_AGENT._run(workflow_id="wf-9")

    assert during == {"status": "running", "host": "host-test"}
    [call] = AgentCall.list_for_run("wf-9")
    assert call.status == "succeeded"
    assert call.sandbox_host_id == "host-test"
    assert call.finished_at is not None


async def test_provisioning_failure_records_no_call(db_session, tmp_path, monkeypatch, current_run):
    """A failure before the agent starts (e.g. no VM capacity) is not an agent
    call — the row only exists once there's a host to run on, so none is recorded."""
    _patch_runtime(monkeypatch, tmp_path, {"ok": True})

    @asynccontextmanager
    async def boom(self, *, idempotency_key=None, **_kwargs):
        raise RuntimeError("no capacity")
        yield  # pragma: no cover

    monkeypatch.setattr("druks.sandbox.client.Client.ephemeral", boom)

    with pytest.raises(RuntimeError, match="no capacity"):
        await DUMMY_AGENT._run(workflow_id="wf-9")

    assert AgentCall.list_for_run("wf-9") == []


async def test_crash_after_start_fails_the_call(db_session, tmp_path, monkeypatch, current_run):
    """A raise after the call started (not a clean FAILED result) closes the
    row instead of leaving it dangling RUNNING."""
    sandbox = _patch_runtime(monkeypatch, tmp_path, {"ok": True})

    async def _boom(**_kwargs):
        raise RuntimeError("kaboom")

    sandbox.run_agent = _boom
    _patch_ephemeral(monkeypatch, sandbox)

    with pytest.raises(RuntimeError, match="kaboom"):
        await DUMMY_AGENT._run(workflow_id="wf-9")

    [call] = AgentCall.list_for_run("wf-9")
    assert call.status == "failed"
    assert "kaboom" in call.last_error


async def test_recovery_supersedes_the_orphaned_running_call(db_session):
    """A worker crash leaves a RUNNING row; the recovered step re-runs with a
    fresh id and abandons the orphan, so the timeline shows one live step."""
    from druks.durable.engine import _step_engine

    engine = _step_engine()
    AgentCall.start(engine, call_id="a", run_id="wf-9", model="m", agent=None, host_id="h")
    AgentCall.start(engine, call_id="b", run_id="wf-9", model="m", agent=None, host_id="h")

    by_id = {call.id: call for call in AgentCall.list_for_run("wf-9")}
    assert by_id["a"].status == "abandoned"
    assert by_id["a"].finished_at is not None
    assert by_id["b"].status == "running"

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from conftest import make_agent_result
from druks import agents
from druks.durable import AgentCall, WorkflowError
from druks.usage.models import UsageScrape


class DummyOutput(agents.AgentOutput):
    ok: bool


DUMMY_AGENT = agents.Agent(
    id="dummy",
    prompt="dummy/agent.md",
    contract=DummyOutput,
    model="claude-haiku-4-5",
)


async def test_get_timeout_caps_at_the_sandbox_lease_max(druks_db):
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

    assert await over.get_timeout() == MAX_AGENT_TIMEOUT_SECONDS
    assert await under.get_timeout() == 600


@pytest.fixture(autouse=True)
async def _seed_run_for_record(druks_db):
    # An agent call records an AgentCall, which FKs to its run.
    from druks.testing import seed_run
    from druks_field_notes.workflows import Summarize

    await seed_run(druks_db, kind=Summarize.kind, run_id="wf-9")


@pytest.fixture(autouse=True)
async def _connected_claude(druks_db):
    # A run refuses to dispatch on an unconnected harness; the runtime tests
    # here resolve to claude models, so connect it once.
    from conftest import connect_harness
    from druks.harnesses.claude import ClaudeHarness

    await connect_harness(ClaudeHarness, {"claudeAiOauth": {"accessToken": "test-token"}})


def _patch_runtime(monkeypatch, tmp_path, payload):
    """Pin settings/prompt; returns a fake sandbox capturing the run_agent
    call. The agent's model/effort/timeout resolve via the override store
    against the test ``druks_db``."""
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

    workflow = Workflow()
    workflow._workflow_id = "wf-9"
    workflow.kind = "test"
    token = current_workflow.set(workflow)
    yield workflow
    current_workflow.reset(token)


@pytest.fixture
def _inline_agent_steps(monkeypatch):
    # Run agent attempts and quota lookups inline while retaining their DBOS step boundaries.
    checkpoints = []

    async def run_inline(options, function):
        checkpoints.append(options)
        return await function()

    sleep = AsyncMock()
    monkeypatch.setattr(agents.DBOS, "run_step_async", run_inline)
    monkeypatch.setattr(agents.DBOS, "sleep_async", sleep)
    return checkpoints, sleep


async def test_run_outside_workflow_raises():
    # an agent call needs a workflow context; standalone agent runs aren't supported yet.
    with pytest.raises(WorkflowError, match="inside a workflow"):
        await DUMMY_AGENT(repo="acme/widget")


async def test_run_refuses_an_agent_with_no_id():
    # Built loose — never assigned to an App, never given an explicit id — so
    # the call it would record would name nobody.
    loose = agents.Agent(prompt="dummy/agent.md", contract=DummyOutput, model="claude")
    with pytest.raises(WorkflowError, match="runs under its own id"):
        await loose(repo="acme/widget")


async def test_run_refuses_unconnected_harness(druks_db, tmp_path, monkeypatch, current_run):
    # The precondition fires where the harness is resolved — before any VM work.
    from druks.accounts.models import Account
    from druks.harnesses.exceptions import HarnessNotConnectedError
    from druks.harnesses.models import HarnessConnection

    await (
        await HarnessConnection.get_for_account(
            "claude", (await Account.get_for_username("op@example.com")).id
        )
    ).delete()
    sandbox = _patch_runtime(monkeypatch, tmp_path, {"ok": True})
    _patch_ephemeral(monkeypatch, sandbox)

    with pytest.raises(HarnessNotConnectedError, match="connect it in Settings"):
        await DUMMY_AGENT._run(workflow_id="wf-9")

    sandbox.run_agent.assert_not_awaited()


async def test_declaration_drives_run_agent_call(druks_db, tmp_path, monkeypatch, current_run):
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


async def test_declared_timeout_is_forwarded(druks_db, tmp_path, monkeypatch, current_run):
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
    druks_db, tmp_path, monkeypatch, current_run
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
    druks_db, tmp_path, monkeypatch, current_run
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


async def test_running_call_visible_then_finished(druks_db, tmp_path, monkeypatch, current_run):
    """The AgentCall exists RUNNING on its host while the agent runs, so the live
    transcript has a row to stream onto, and is finished once it returns."""
    sandbox = _patch_runtime(monkeypatch, tmp_path, {"ok": True})
    during: dict[str, object] = {}

    async def _run_agent(*, call_id, **_kwargs):
        row = await AgentCall.get(call_id)
        during["status"] = row.status
        during["host"] = row.sandbox_host_id
        return make_agent_result({"ok": True}, agent="dummy")

    sandbox.run_agent = _run_agent
    _patch_ephemeral(monkeypatch, sandbox)

    await DUMMY_AGENT._run(workflow_id="wf-9")

    assert during == {"status": "running", "host": "host-test"}
    [call] = await AgentCall.list_for_run("wf-9")
    assert call.status == "succeeded"
    assert call.sandbox_host_id == "host-test"
    assert call.finished_at is not None


async def test_provisioning_failure_records_no_call(druks_db, tmp_path, monkeypatch, current_run):
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

    assert await AgentCall.list_for_run("wf-9") == []


async def test_crash_after_start_fails_the_call(druks_db, tmp_path, monkeypatch, current_run):
    """A raise after the call started (not a clean FAILED result) closes the
    row instead of leaving it dangling RUNNING."""
    sandbox = _patch_runtime(monkeypatch, tmp_path, {"ok": True})

    async def _boom(**_kwargs):
        raise RuntimeError("kaboom")

    sandbox.run_agent = _boom
    _patch_ephemeral(monkeypatch, sandbox)

    with pytest.raises(RuntimeError, match="kaboom"):
        await DUMMY_AGENT._run(workflow_id="wf-9")

    [call] = await AgentCall.list_for_run("wf-9")
    assert call.status == "failed"
    assert "kaboom" in call.last_error


async def test_a_carried_failure_is_raised_with_its_code(
    druks_db, tmp_path, monkeypatch, current_run
):
    """The sandbox captures a harness failure on the result so the call can
    record its cost; the agent call then re-raises that same error, and the row
    keeps the code a retry decides on."""
    from druks.harnesses.exceptions import HarnessOverloadedError

    sandbox = _patch_runtime(monkeypatch, tmp_path, {"ok": True})
    overloaded = HarnessOverloadedError("claude exited with 1. API Error: 529 Overloaded.")

    async def _run_agent(**_kwargs):
        return make_agent_result(None, agent="dummy", error=overloaded)

    sandbox.run_agent = _run_agent
    _patch_ephemeral(monkeypatch, sandbox)

    with pytest.raises(HarnessOverloadedError) as excinfo:
        await DUMMY_AGENT._run(workflow_id="wf-9")

    assert excinfo.value is overloaded
    [call] = await AgentCall.list_for_run("wf-9")
    assert call.status == "failed"
    assert call.failure_code == "overloaded"
    assert call.last_error == (
        "dummy: HarnessOverloadedError: claude exited with 1. API Error: 529 Overloaded."
    )


async def test_body_level_overload_retries_as_separate_durable_attempts(
    druks_db, tmp_path, monkeypatch, current_run, _inline_agent_steps
):
    from druks.harnesses.exceptions import HarnessOverloadedError

    failures = [
        HarnessOverloadedError("overloaded once"),
        HarnessOverloadedError("overloaded twice"),
    ]
    sandbox = _patch_runtime(monkeypatch, tmp_path, {"ok": True})
    sandbox.run_agent = AsyncMock(
        side_effect=[
            make_agent_result(None, agent="dummy", error=failures[0]),
            make_agent_result(None, agent="dummy", error=failures[1]),
            make_agent_result({"ok": True}, agent="dummy"),
        ]
    )
    _patch_ephemeral(monkeypatch, sandbox)
    monkeypatch.setattr(agents.random, "uniform", MagicMock(side_effect=[0.95, 1.05]))
    current_run._reap_run = AsyncMock()
    checkpoints, sleep = _inline_agent_steps

    result = await DUMMY_AGENT()

    assert result == DummyOutput(ok=True)
    assert [awaited.args[0] for awaited in sleep.await_args_list] == [285.0, 945.0]
    assert [options["name"] for options in checkpoints] == ["test.agent.dummy"] * 3
    assert current_run._reap_run.await_count == 2
    calls = await AgentCall.list_for_run("wf-9")
    assert len(calls) == 3
    assert sum(call.status == "failed" for call in calls) == 2
    assert sum(call.status == "succeeded" for call in calls) == 1
    assert {call.failure_code for call in calls if call.status == "failed"} == {"overloaded"}
    assert current_run.journal.filter(DummyOutput) == [result]


async def test_body_level_first_byte_retries_immediately_then_reraises(
    druks_db, tmp_path, monkeypatch, current_run, _inline_agent_steps
):
    from druks.harnesses.exceptions import HarnessFirstByteTimeoutError

    failures = [HarnessFirstByteTimeoutError(f"wedged {attempt}") for attempt in range(3)]
    sandbox = _patch_runtime(monkeypatch, tmp_path, {"ok": True})
    sandbox.run_agent = AsyncMock(
        side_effect=[make_agent_result(None, agent="dummy", error=failure) for failure in failures]
    )
    _patch_ephemeral(monkeypatch, sandbox)
    monkeypatch.setattr(agents.random, "uniform", lambda _low, _high: 1.0)
    current_run._reap_run = AsyncMock()
    _, sleep = _inline_agent_steps

    with pytest.raises(HarnessFirstByteTimeoutError) as excinfo:
        await DUMMY_AGENT()

    assert excinfo.value is failures[-1]
    assert excinfo.value.code == "first_byte"
    assert [awaited.args[0] for awaited in sleep.await_args_list] == [0.0, 0.0]
    current_run._reap_run.assert_not_awaited()
    calls = await AgentCall.list_for_run("wf-9")
    assert len(calls) == 3
    assert all(call.status == "failed" for call in calls)


def _async_scrape(make):
    async def latest_for(_cls, _harness, _account_id):
        return make()

    return latest_for


async def test_body_level_quota_waits_for_the_reset_once(
    druks_db, tmp_path, monkeypatch, current_run, _inline_agent_steps
):
    from druks.harnesses.exceptions import HarnessRateLimitError

    now = datetime(2026, 8, 1, 10, tzinfo=UTC)
    failures = [HarnessRateLimitError(f"quota {attempt}") for attempt in range(2)]
    sandbox = _patch_runtime(monkeypatch, tmp_path, {"ok": True})
    sandbox.run_agent = AsyncMock(
        side_effect=[make_agent_result(None, agent="dummy", error=failure) for failure in failures]
    )
    _patch_ephemeral(monkeypatch, sandbox)
    clock = MagicMock()
    clock.now.return_value = now
    monkeypatch.setattr(agents, "datetime", clock)
    monkeypatch.setattr(
        agents.UsageScrape,
        "latest_for",
        classmethod(
            _async_scrape(
                lambda: UsageScrape(
                    five_hour_resets_at=now + timedelta(hours=2),
                    weeks=[
                        {
                            "percent_left": 0,
                            "resets_at": (now + timedelta(hours=1)).isoformat(),
                            "model": "Fable",
                        },
                        {
                            "percent_left": 20,
                            "resets_at": (now + timedelta(days=1)).isoformat(),
                            "model": None,
                        },
                    ],
                )
            )
        ),
    )
    jitter = MagicMock(return_value=90.0)
    monkeypatch.setattr(agents.random, "uniform", jitter)
    current_run._reap_run = AsyncMock()
    checkpoints, sleep = _inline_agent_steps

    with pytest.raises(HarnessRateLimitError) as excinfo:
        await DUMMY_AGENT()

    assert excinfo.value is failures[-1]
    sleep.assert_awaited_once_with(60 * 60 + 90.0)
    jitter.assert_called_once_with(60.0, 120.0)
    current_run._reap_run.assert_awaited_once()
    assert [options["name"] for options in checkpoints] == [
        "test.agent.dummy",
        "test.agent.dummy.retry_wait",
        "test.agent.dummy",
    ]
    calls = await AgentCall.list_for_run("wf-9")
    assert len(calls) == 2
    assert all(call.failure_code == "rate_limited" for call in calls)


async def test_body_level_quota_reset_over_six_hours_reraises_without_sleeping(
    druks_db, tmp_path, monkeypatch, current_run, _inline_agent_steps
):
    from druks.harnesses.exceptions import HarnessUsageLimitError

    now = datetime(2026, 8, 1, 10, tzinfo=UTC)
    failure = HarnessUsageLimitError("weekly quota")
    sandbox = _patch_runtime(monkeypatch, tmp_path, {"ok": True})
    sandbox.run_agent = AsyncMock(
        return_value=make_agent_result(None, agent="dummy", error=failure)
    )
    _patch_ephemeral(monkeypatch, sandbox)
    clock = MagicMock()
    clock.now.return_value = now
    monkeypatch.setattr(agents, "datetime", clock)
    monkeypatch.setattr(
        agents.UsageScrape,
        "latest_for",
        classmethod(
            _async_scrape(
                lambda: UsageScrape(
                    five_hour_resets_at=now + timedelta(hours=7),
                    weeks=[
                        {
                            "percent_left": 0,
                            "resets_at": (now + timedelta(days=1)).isoformat(),
                            "model": None,
                        }
                    ],
                )
            )
        ),
    )
    jitter = MagicMock()
    monkeypatch.setattr(agents.random, "uniform", jitter)
    current_run._reap_run = AsyncMock()
    _, sleep = _inline_agent_steps

    with pytest.raises(HarnessUsageLimitError) as excinfo:
        await DUMMY_AGENT()

    assert excinfo.value is failure
    sleep.assert_not_awaited()
    jitter.assert_not_called()
    current_run._reap_run.assert_not_awaited()
    [call] = await AgentCall.list_for_run("wf-9")
    assert call.failure_code == "usage_limit"


@pytest.mark.parametrize("error_name", [pytest.param("bare"), pytest.param("auth")])
async def test_body_level_never_retry_errors_run_once(
    error_name, druks_db, tmp_path, monkeypatch, current_run, _inline_agent_steps
):
    from druks.harnesses.exceptions import HarnessAuthError, HarnessError, Retry

    failures = {"bare": HarnessError, "auth": HarnessAuthError}
    failure = failures[error_name]("terminal")
    sandbox = _patch_runtime(monkeypatch, tmp_path, {"ok": True})
    sandbox.run_agent = AsyncMock(
        return_value=make_agent_result(None, agent="dummy", error=failure)
    )
    _patch_ephemeral(monkeypatch, sandbox)
    current_run._reap_run = AsyncMock()
    checkpoints, sleep = _inline_agent_steps

    with pytest.raises(type(failure)) as excinfo:
        await DUMMY_AGENT()

    assert excinfo.value is failure
    assert failure.retry is Retry.NEVER
    assert [options["name"] for options in checkpoints] == ["test.agent.dummy"]
    sleep.assert_not_awaited()
    current_run._reap_run.assert_not_awaited()
    sandbox.run_agent.assert_awaited_once()
    assert len(await AgentCall.list_for_run("wf-9")) == 1


async def test_in_step_transient_retry_uses_asyncio_sleep(monkeypatch, current_run):
    from druks.harnesses.exceptions import HarnessOverloadedError
    from druks.workflows import _in_step

    attempts = 0

    async def run_agent(_self, **_context):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise HarnessOverloadedError("overloaded")
        return DummyOutput(ok=True)

    monkeypatch.setattr(agents.Agent, "_run", run_agent)
    monkeypatch.setattr(agents.random, "uniform", lambda _low, _high: 1.0)
    memory_sleep = AsyncMock()
    durable_sleep = AsyncMock()
    monkeypatch.setattr(agents.asyncio, "sleep", memory_sleep)
    monkeypatch.setattr(agents.DBOS, "sleep_async", durable_sleep)
    token = _in_step.set(True)
    try:
        result = await DUMMY_AGENT()
    finally:
        _in_step.reset(token)

    assert result == DummyOutput(ok=True)
    assert attempts == 2
    memory_sleep.assert_awaited_once_with(300.0)
    durable_sleep.assert_not_awaited()


async def test_in_step_quota_reraises_without_sleeping(monkeypatch, current_run):
    from druks.harnesses.exceptions import HarnessRateLimitError
    from druks.workflows import _in_step

    failure = HarnessRateLimitError("quota")

    async def run_agent(_self, **_context):
        raise failure

    monkeypatch.setattr(agents.Agent, "_run", run_agent)
    memory_sleep = AsyncMock()
    durable_sleep = AsyncMock()
    monkeypatch.setattr(agents.asyncio, "sleep", memory_sleep)
    monkeypatch.setattr(agents.DBOS, "sleep_async", durable_sleep)
    token = _in_step.set(True)
    try:
        with pytest.raises(HarnessRateLimitError) as excinfo:
            await DUMMY_AGENT()
    finally:
        _in_step.reset(token)

    assert excinfo.value is failure
    memory_sleep.assert_not_awaited()
    durable_sleep.assert_not_awaited()


def _flaky_ephemeral(sandbox, keys, *, failures):
    """An ephemeral() that raises each error in ``failures`` in turn (recording
    the idempotency key each attempt presents) then yields ``sandbox``. Used to
    simulate a transient provisioning failure at acquire time that recovers."""
    attempts = 0

    @asynccontextmanager
    async def _fake(self, *, idempotency_key=None, **_kwargs):
        nonlocal attempts
        keys.append(idempotency_key)
        index = attempts
        attempts += 1
        if index < len(failures):
            raise failures[index]
        yield sandbox

    return _fake


async def test_provisioning_failure_recovers_through_durable_retry(
    druks_db, tmp_path, monkeypatch, current_run, _inline_agent_steps
):
    """A transient provisioning failure at acquire time is retried by the
    body-level durable path with backoff; a later attempt succeeds, and every
    attempt for the one logical acquire presents the same ephemeral key."""
    from druks.harnesses.exceptions import HarnessSandboxProvisioningError

    sandbox = _patch_runtime(monkeypatch, tmp_path, {"ok": True})
    keys: list[str | None] = []
    monkeypatch.setattr(
        "druks.sandbox.client.Client.ephemeral",
        _flaky_ephemeral(
            sandbox, keys, failures=[HarnessSandboxProvisioningError("exe.dev create timed out")]
        ),
    )
    monkeypatch.setattr(agents.random, "uniform", lambda _low, _high: 1.0)
    current_run._reap_run = AsyncMock()
    checkpoints, sleep = _inline_agent_steps

    result = await DUMMY_AGENT()

    assert result == DummyOutput(ok=True)
    # First delay off the schedule HarnessSandboxProvisioningError inherits (60, 300).
    assert [awaited.args[0] for awaited in sleep.await_args_list] == [60.0]
    # Each attempt re-enters _run with the same workflow/agent identity → same key.
    assert keys == ["wf-9:dummy", "wf-9:dummy"]
    # A 60s wait is under the reap-before threshold, so the (never-acquired) VM
    # isn't reaped between attempts.
    current_run._reap_run.assert_not_awaited()
    # Both attempts are separate durable steps under the agent's step name.
    assert [options["name"] for options in checkpoints] == ["test.agent.dummy"] * 2


async def test_provisioning_failure_recovers_in_step_retry(
    druks_db, tmp_path, monkeypatch, current_run
):
    """An agent invoked inside an enclosing @step retries the same transient
    provisioning failure in memory (plain asyncio.sleep, no durable sleep) and
    recovers, holding the ephemeral key stable across attempts."""
    from druks.harnesses.exceptions import HarnessSandboxProvisioningError
    from druks.workflows import _in_step

    sandbox = _patch_runtime(monkeypatch, tmp_path, {"ok": True})
    keys: list[str | None] = []
    monkeypatch.setattr(
        "druks.sandbox.client.Client.ephemeral",
        _flaky_ephemeral(
            sandbox, keys, failures=[HarnessSandboxProvisioningError("exe.dev create timed out")]
        ),
    )
    monkeypatch.setattr(agents.random, "uniform", lambda _low, _high: 1.0)
    memory_sleep = AsyncMock()
    durable_sleep = AsyncMock()
    monkeypatch.setattr(agents.asyncio, "sleep", memory_sleep)
    monkeypatch.setattr(agents.DBOS, "sleep_async", durable_sleep)

    token = _in_step.set(True)
    try:
        result = await DUMMY_AGENT()
    finally:
        _in_step.reset(token)

    assert result == DummyOutput(ok=True)
    memory_sleep.assert_awaited_once_with(60.0)
    durable_sleep.assert_not_awaited()
    assert keys == ["wf-9:dummy", "wf-9:dummy"]


async def test_reused_host_retry_presents_a_stable_idempotency_key(monkeypatch, current_run):
    """A warm workflow that reuses its sandbox re-provisions the host on the
    same logical acquire with an unchanged idempotency key, so an ambiguous
    create timeout resolves to the existing host instead of leaking a duplicate."""
    from druks.harnesses.exceptions import HarnessSandboxProvisioningError

    current_run.steps_reuse_sandbox = True
    keys: list[str | None] = []
    host = MagicMock()
    host.id = "warm-host"
    host.expires_at = None
    attempts = 0

    async def fake_provision(self, *, idempotency_key=None, **_kwargs):
        nonlocal attempts
        keys.append(idempotency_key)
        index = attempts
        attempts += 1
        if index == 0:
            raise HarnessSandboxProvisioningError("provider slow")
        return host

    monkeypatch.setattr("druks.sandbox.client.Client.provision", fake_provision)

    with pytest.raises(HarnessSandboxProvisioningError):
        await current_run._ensure_host()
    host_id = await current_run._ensure_host()

    assert host_id == "warm-host"
    assert keys == ["wf-9:sandbox", "wf-9:sandbox"]


async def test_provisioning_failure_exhausts_retries_with_classified_code(
    druks_db, tmp_path, monkeypatch, current_run, _inline_agent_steps
):
    """When transient provisioning retries exhaust, the propagated error still
    carries ``code=sandbox_provisioning`` — the classification survives to the
    run's failure record instead of falling back to an empty code."""
    from druks.harnesses.exceptions import HarnessSandboxProvisioningError

    _patch_runtime(monkeypatch, tmp_path, {"ok": True})
    failures = [HarnessSandboxProvisioningError(f"provider down {i}") for i in range(3)]
    monkeypatch.setattr(
        "druks.sandbox.client.Client.ephemeral", _flaky_ephemeral(None, [], failures=failures)
    )
    monkeypatch.setattr(agents.random, "uniform", lambda _low, _high: 1.0)
    current_run._reap_run = AsyncMock()
    _, sleep = _inline_agent_steps

    with pytest.raises(HarnessSandboxProvisioningError) as excinfo:
        await DUMMY_AGENT()

    assert excinfo.value is failures[-1]
    assert excinfo.value.code == "sandbox_provisioning"
    # One sleep per inherited delay; past the schedule the failure stands.
    assert [awaited.args[0] for awaited in sleep.await_args_list] == [60.0, 300.0]
    # The 300s wait is over the reap-before threshold, so the run is reaped once.
    assert current_run._reap_run.await_count == 1


async def test_fatal_sdk_error_does_not_trigger_agent_retry(
    druks_db, tmp_path, monkeypatch, current_run, _inline_agent_steps
):
    """A non-transient SDK failure surfacing from acquire is not a HarnessError,
    so it propagates on the first attempt with no retry sleep — auth/validation
    failures can't recover without changed inputs."""
    from drukbox_sdk.exceptions import SandboxValidationError

    _patch_runtime(monkeypatch, tmp_path, {"ok": True})
    fatal = SandboxValidationError("bad image")
    attempts = 0

    @asynccontextmanager
    async def fatal_ephemeral(self, *, idempotency_key=None, **_kwargs):
        nonlocal attempts
        attempts += 1
        raise fatal
        yield  # pragma: no cover

    monkeypatch.setattr("druks.sandbox.client.Client.ephemeral", fatal_ephemeral)
    current_run._reap_run = AsyncMock()
    _, sleep = _inline_agent_steps

    with pytest.raises(SandboxValidationError) as excinfo:
        await DUMMY_AGENT()

    assert excinfo.value is fatal
    assert attempts == 1
    sleep.assert_not_awaited()
    current_run._reap_run.assert_not_awaited()


async def test_recovery_supersedes_the_orphaned_running_call(druks_db):
    """A worker crash leaves a RUNNING row; the recovered step re-runs with a
    fresh id and abandons the orphan, so the timeline shows one live step."""
    from druks.durable.engine import _step_engine

    engine = _step_engine()
    await AgentCall.start(
        engine,
        call_id="a",
        run_id="wf-9",
        model="m",
        agent="summarize",
        host_id="h",
        account_id="system",
    )
    await AgentCall.start(
        engine,
        call_id="b",
        run_id="wf-9",
        model="m",
        agent="summarize",
        host_id="h",
        account_id="system",
    )

    by_id = {call.id: call for call in await AgentCall.list_for_run("wf-9")}
    assert by_id["a"].status == "abandoned"
    assert by_id["a"].finished_at is not None
    assert by_id["b"].status == "running"

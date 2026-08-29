from druks.accounts.models import Account
from druks.durable.enums import RunState
from druks.durable.exceptions import GateTimeout
from druks.durable.models import AgentCall, Run
from druks.durable.reads import _status
from druks_field_notes.workflows import Summarize


def _run(
    id: str,
    kind: str,
    state: RunState,
    input_gate: str | None = None,
    failure: str | None = None,
    failure_code: str | None = None,
    agent_calls: list[AgentCall] | None = None,
) -> Run:
    return Run(
        id=id,
        kind=kind,
        state=state.value,
        input_gate=input_gate,
        failure=failure,
        failure_code=failure_code,
        account=Account(username="op@example.com"),
        agent_calls=agent_calls or [],
    )


async def _status_of(runs):
    # runs arrives newest-first, mirroring Run.list_for_subject.
    return await _status(runs[0])


async def test_subject_state_takes_the_newest_run():
    runs = [
        _run("new", "software_factory.build", RunState.RUNNING),
        _run("old", "software_factory.build", RunState.PARKED),
    ]
    assert (await _status_of(runs)).state == RunState.RUNNING


async def test_subject_state_takes_a_newer_parked_run_over_an_older_running_one():
    # Recency decides, not a hardcoded state preference.
    runs = [
        _run("new", "software_factory.build", RunState.PARKED),
        _run("old", "software_factory.build", RunState.RUNNING),
    ]
    assert (await _status_of(runs)).state == RunState.PARKED


async def test_subject_state_uses_the_latest_outcome_once_every_run_is_terminal():
    runs = [
        _run("new", "software_factory.build", RunState.FINISHED),
        _run("old", "software_factory.build", RunState.FAILED),
    ]
    assert (await _status_of(runs)).state == RunState.FINISHED


async def test_status_surfaces_the_newest_active_runs_gate():
    runs = [
        _run("new", "software_factory.build", RunState.PARKED, "review"),
        _run("old", "software_factory.build", RunState.PARKED, "review_work"),
    ]
    assert (await _status_of(runs)).gate == "review"


async def test_status_carries_the_running_runs_kind_and_no_stale_gate():
    runs = [
        _run("new", "software_factory.build", RunState.RUNNING),
        _run("old", "software_factory.build", RunState.PARKED, "review"),
    ]
    status = await _status_of(runs)
    assert status.kind == "software_factory.build"
    assert not status.gate


async def test_status_carries_the_latest_agent_call_agent():
    runs = [
        _run(
            "new",
            "software_factory.build",
            RunState.RUNNING,
            agent_calls=[AgentCall(agent="generate_plan"), AgentCall(agent="implement")],
        )
    ]
    assert (await _status_of(runs)).agent == "implement"


async def test_parked_status_carries_no_agent_even_when_the_run_has_calls():
    # A parked run keeps its calls, but the status never reads them — the fact
    # stays consistent with the board, where a parked row never queries them.
    runs = [
        _run(
            "new",
            "software_factory.build",
            RunState.PARKED,
            "review",
            agent_calls=[AgentCall(agent="implement")],
        )
    ]
    status = await _status_of(runs)
    assert not status.agent
    assert status.gate == "review"


async def test_status_carries_the_gate_timeout_reason():
    # The gate timeout's stamped failure_code rides the status as ``reason`` —
    # the board renders the re-trigger hint from it instead of a bare "failed".
    runs = [
        _run("new", Summarize.kind, RunState.FAILED, failure_code=GateTimeout.code),
    ]
    status = await _status_of(runs)
    assert status.reason == GateTimeout.code
    assert status.kind == Summarize.kind


async def test_status_carries_failure_but_no_reason_when_the_run_crashed():
    runs = [
        _run("new", Summarize.kind, RunState.FAILED, failure="boom"),
    ]
    status = await _status_of(runs)
    assert not status.reason
    assert status.failure == "boom"


async def test_status_falls_back_to_the_terminal_call_error_when_failure_is_null():
    # A crash leaves run.failure null; the terminal call's captured error becomes
    # the status reason so the board never shows a bare "failed".
    runs = [
        _run(
            "new",
            Summarize.kind,
            RunState.FAILED,
            agent_calls=[AgentCall(agent="implement", last_error="crashed in implement")],
        )
    ]
    assert (await _status_of(runs)).failure == "crashed in implement"

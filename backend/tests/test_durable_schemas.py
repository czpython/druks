from druks.durable.enums import RunState
from druks.durable.exceptions import GateTimeout
from druks.durable.models import AgentCall, Run
from druks.durable.reads import _status


def _run(
    id: str,
    kind: str,
    state: RunState,
    input_gate: str | None = None,
    failure: str | None = None,
    failure_code: str | None = None,
) -> Run:
    return Run(
        id=id,
        kind=kind,
        state=state.value,
        input_gate=input_gate,
        failure=failure,
        failure_code=failure_code,
    )


def _status_of(runs, active_calls=None):
    active_run = next((run for run in runs if run.is_active), None)
    return _status(runs, active_run, active_calls or [])


def test_subject_state_prefers_the_newer_active_run_over_a_stale_parked_one():
    # runs arrives newest-first, mirroring Run.list_for_subject.
    runs = [
        _run("new", "build.build_workflow", RunState.RUNNING),
        _run("old", "build.build_workflow", RunState.PENDING_INPUT),
    ]
    assert _status_of(runs).state == RunState.RUNNING


def test_subject_state_prefers_a_newer_parked_run_over_an_older_running_one():
    # Recency decides, not a hardcoded state preference.
    runs = [
        _run("new", "build.build_workflow", RunState.PENDING_INPUT),
        _run("old", "build.build_workflow", RunState.RUNNING),
    ]
    assert _status_of(runs).state == RunState.PENDING_INPUT


def test_subject_state_uses_the_latest_outcome_once_every_run_is_terminal():
    runs = [
        _run("new", "build.build_workflow", RunState.FINISHED),
        _run("old", "build.build_workflow", RunState.FAILED),
    ]
    assert _status_of(runs).state == RunState.FINISHED


def test_status_surfaces_the_newest_active_runs_gate():
    runs = [
        _run("new", "build.build_workflow", RunState.PENDING_INPUT, "review"),
        _run("old", "build.build_workflow", RunState.PENDING_INPUT, "scope_reply"),
    ]
    assert _status_of(runs).gate == "review"


def test_status_carries_the_running_runs_kind_and_no_stale_gate():
    runs = [
        _run("new", "build.build_workflow", RunState.RUNNING),
        _run("old", "build.build_workflow", RunState.PENDING_INPUT, "review"),
    ]
    status = _status_of(runs)
    assert status.kind == "build.build_workflow"
    assert not status.gate


def test_status_carries_the_latest_agent_call_agent():
    runs = [_run("new", "build.build_workflow", RunState.RUNNING)]
    calls = [AgentCall(agent="generate_plan"), AgentCall(agent="implement")]
    assert _status_of(runs, calls).agent == "implement"


def test_parked_status_carries_no_agent_even_when_calls_are_handed_in():
    # The detail read passes the parked run's calls; the fact stays consistent
    # with the board, where a parked row never queries them.
    runs = [
        _run("new", "build.build_workflow", RunState.PENDING_INPUT, "review"),
    ]
    calls = [AgentCall(agent="implement")]
    status = _status_of(runs, calls)
    assert not status.agent
    assert status.gate == "review"


def test_status_carries_the_gate_timeout_reason():
    # The gate timeout's stamped failure_code rides the status as ``reason`` —
    # the board renders the re-trigger hint from it instead of a bare "failed".
    runs = [
        _run("new", "build.scope", RunState.FAILED, failure_code=GateTimeout.code),
    ]
    status = _status_of(runs)
    assert status.reason == GateTimeout.code
    assert status.kind == "build.scope"


def test_status_carries_failure_but_no_reason_when_the_run_crashed():
    runs = [
        _run("new", "build.scope", RunState.FAILED, failure="boom"),
    ]
    status = _status_of(runs)
    assert not status.reason
    assert status.failure == "boom"

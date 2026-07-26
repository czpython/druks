from druks.contrib.ship.workflows import Build
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


def _status_of(runs, calls=None):
    # runs arrives newest-first, mirroring Run.list_for_subject.
    return _status(runs[0], calls or [])


def test_subject_state_takes_the_newest_run():
    runs = [
        _run("new", "ship.build", RunState.RUNNING),
        _run("old", "ship.build", RunState.PARKED),
    ]
    assert _status_of(runs).state == RunState.RUNNING


def test_subject_state_takes_a_newer_parked_run_over_an_older_running_one():
    # Recency decides, not a hardcoded state preference.
    runs = [
        _run("new", "ship.build", RunState.PARKED),
        _run("old", "ship.build", RunState.RUNNING),
    ]
    assert _status_of(runs).state == RunState.PARKED


def test_subject_state_uses_the_latest_outcome_once_every_run_is_terminal():
    runs = [
        _run("new", "ship.build", RunState.FINISHED),
        _run("old", "ship.build", RunState.FAILED),
    ]
    assert _status_of(runs).state == RunState.FINISHED


def test_status_surfaces_the_newest_active_runs_gate():
    runs = [
        _run("new", "ship.build", RunState.PARKED, "review"),
        _run("old", "ship.build", RunState.PARKED, "review_work"),
    ]
    assert _status_of(runs).gate == "review"


def test_status_carries_the_running_runs_kind_and_no_stale_gate():
    runs = [
        _run("new", "ship.build", RunState.RUNNING),
        _run("old", "ship.build", RunState.PARKED, "review"),
    ]
    status = _status_of(runs)
    assert status.kind == "ship.build"
    assert not status.gate


def test_status_carries_the_latest_agent_call_agent():
    runs = [_run("new", "ship.build", RunState.RUNNING)]
    calls = [AgentCall(agent="generate_plan"), AgentCall(agent="implement")]
    assert _status_of(runs, calls).agent == "implement"


def test_parked_status_carries_no_agent_even_when_calls_are_handed_in():
    # The detail read passes the parked run's calls; the fact stays consistent
    # with the board, where a parked row never queries them.
    runs = [
        _run("new", "ship.build", RunState.PARKED, "review"),
    ]
    calls = [AgentCall(agent="implement")]
    status = _status_of(runs, calls)
    assert not status.agent
    assert status.gate == "review"


def test_status_carries_the_gate_timeout_reason():
    # The gate timeout's stamped failure_code rides the status as ``reason`` —
    # the board renders the re-trigger hint from it instead of a bare "failed".
    runs = [
        _run("new", Build.kind, RunState.FAILED, failure_code=GateTimeout.code),
    ]
    status = _status_of(runs)
    assert status.reason == GateTimeout.code
    assert status.kind == Build.kind


def test_status_carries_failure_but_no_reason_when_the_run_crashed():
    runs = [
        _run("new", Build.kind, RunState.FAILED, failure="boom"),
    ]
    status = _status_of(runs)
    assert not status.reason
    assert status.failure == "boom"

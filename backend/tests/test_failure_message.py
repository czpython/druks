from types import SimpleNamespace

from druks.durable.enums import RunState
from druks.durable.models import Run


def _run(failure=None, state=RunState.FAILED.value, agent_calls=()):
    return SimpleNamespace(failure=failure, state=state, agent_calls=list(agent_calls))


def test_explicit_failure_wins():
    assert Run.failure_message(_run(failure="gate timed out")) == "gate timed out"


def test_crash_falls_back_to_terminal_call_error():
    run = _run(agent_calls=[SimpleNamespace(last_error="boom in step two")])
    assert Run.failure_message(run) == "boom in step two"


def test_running_run_stays_none():
    assert Run.failure_message(_run(state=RunState.RUNNING.value)) is None


def test_failed_without_calls_stays_none():
    assert Run.failure_message(_run()) is None

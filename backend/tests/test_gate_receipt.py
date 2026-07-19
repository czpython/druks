import pytest
from conftest import seed_run
from dbos import DBOS
from dbos._error import DBOSWorkflowCancelledError
from druks.durable.exceptions import GateTimeout
from druks.durable.models import Run
from druks.workflows import _park

_ASK = {"presentation": "in_app", "controls": ["approve"], "questions": []}


class _ParkedWorkflow:
    # The slice of Workflow that _park touches. subject=None keeps the emit to
    # its facts write (no feed event, no notification) — the receipt path under
    # test is exactly that write.
    def __init__(self, workflow_id: str) -> None:
        self.workflow_id = workflow_id
        self.subject = None

    async def _reap_run(self) -> None:
        return


@pytest.fixture
def _direct_steps(monkeypatch):
    # Run each durable step body inline — the test exercises _park's own logic,
    # not DBOS checkpointing.
    async def _call_through(options, func, *args, **kwargs):
        return await func(*args, **kwargs)

    monkeypatch.setattr(DBOS, "run_step_async", _call_through)


def _reload(db_session, run_id: str) -> Run:
    db_session.expire_all()
    return db_session.get(Run, run_id)


async def test_answer_stamps_the_receipt_beside_the_gate_clear(
    db_session, _direct_steps, monkeypatch
):
    run = seed_run(db_session, "run-receipt-answer")

    async def _answer(topic, timeout_seconds):
        return {"action": "approve"}

    monkeypatch.setattr(DBOS, "recv_async", _answer)
    payload = await _park(_ParkedWorkflow(run.id), "review", _ASK, ttl_seconds=1.0)

    assert payload == {"action": "approve"}
    run = _reload(db_session, run.id)
    # The receipt is the round the answer cleared: the same stamp the park
    # wrote, which _GATE_CLEARED preserves on the row.
    assert run.input_requested_at
    assert run.answered_parked_at == run.input_requested_at
    assert not run.input_gate
    assert not run.input_request


async def test_timeout_never_writes_the_receipt(db_session, _direct_steps, monkeypatch):
    run = seed_run(db_session, "run-receipt-timeout")

    async def _lapse(topic, timeout_seconds):
        return None

    monkeypatch.setattr(DBOS, "recv_async", _lapse)
    with pytest.raises(GateTimeout):
        await _park(_ParkedWorkflow(run.id), "review", _ASK, ttl_seconds=1.0)

    run = _reload(db_session, run.id)
    assert not run.answered_parked_at
    assert run.input_requested_at


async def test_cancel_never_writes_the_receipt(db_session, _direct_steps, monkeypatch):
    run = seed_run(db_session, "run-receipt-cancel")

    async def _cancelled(topic, timeout_seconds):
        raise DBOSWorkflowCancelledError(run.id)

    monkeypatch.setattr(DBOS, "recv_async", _cancelled)
    with pytest.raises(DBOSWorkflowCancelledError):
        await _park(_ParkedWorkflow(run.id), "review", _ASK, ttl_seconds=1.0)

    run = _reload(db_session, run.id)
    assert not run.answered_parked_at

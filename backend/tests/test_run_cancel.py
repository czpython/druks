import pytest
from conftest import make_test_work_item, seed_build_run
from druks.durable.dbos_state import workflow_status
from druks.durable.enums import RunState
from sqlalchemy import update


@pytest.mark.asyncio
async def test_cancel_frees_subject_immediately(db_session, monkeypatch):
    # A cancel while parked must not wedge the subject until GATE_TTL: it clears
    # the gate and cancels the DBOS workflow — which dequeues it, frees the
    # subject's dedup slot, and writes the CANCELLED status that derived state
    # reads. The stub plays DBOS's half so the derivation is observable.
    cancelled: list[str] = []

    async def _dbos_cancel(workflow_id: str) -> None:
        cancelled.append(workflow_id)
        db_session.execute(
            update(workflow_status)
            .where(workflow_status.c.workflow_uuid == workflow_id)
            .values(status="CANCELLED")
        )

    monkeypatch.setattr("dbos.DBOS.cancel_workflow_async", _dbos_cancel)
    item = make_test_work_item(repo="ClawHaven/acme-app", title="Cancelled while parked")
    run = seed_build_run(
        db_session,
        work_item_id=item.id,
        state="parked",
        input_gate="review_work",
        input_request={"label": "Review", "presentation": "in_app"},
    )

    await run.cancel(failure="pr merged while parked")

    # cancel() never writes state — the already-loaded Run still carries the old
    # one until expired/re-selected, which is exactly what responses must do.
    # (cancel flushes the ambient session; the fixture session holds `run`.)
    db_session.flush()
    db_session.expire(run)
    assert run.state == RunState.CANCELLED.value
    assert run.input_gate is None
    assert run.input_request is None
    assert run.failure == "pr merged while parked"
    assert cancelled == [run.id]

import pytest
from druks.durable.dbos_state import workflow_status
from druks.durable.enums import RunState
from druks.testing import seed_run
from druks_field_notes.models import Note
from druks_field_notes.workflows import Summarize
from sqlalchemy import update


@pytest.mark.asyncio
async def test_cancel_frees_subject_immediately(druks_db, monkeypatch):
    # A cancel while parked must not wedge the subject until GATE_TTL: it clears
    # the gate and cancels the DBOS workflow — which dequeues it, frees the
    # subject's dedup slot, and writes the CANCELLED status that derived state
    # reads. The stub plays DBOS's half so the derivation is observable.
    cancelled: list[str] = []

    async def _dbos_cancel(workflow_id: str) -> None:
        cancelled.append(workflow_id)
        await druks_db.execute(
            update(workflow_status)
            .where(workflow_status.c.workflow_uuid == workflow_id)
            .values(status="CANCELLED")
        )

    monkeypatch.setattr("dbos.DBOS.cancel_workflow_async", _dbos_cancel)
    note = await Note.create(body="cancelled while parked")
    run = await seed_run(
        druks_db,
        kind=Summarize.kind,
        subject=note,
        state="parked",
        input_gate="review_work",
        input_request={"label": "Review", "presentation": "in_app"},
    )

    await run.cancel(failure="pr merged while parked")

    # cancel() never writes state — the already-loaded Run still carries the old
    # one until expired/re-selected, which is exactly what responses must do.
    # (cancel flushes the ambient session; the fixture session holds `run`.)
    await druks_db.flush()
    await druks_db.refresh(run)
    assert run.state == RunState.CANCELLED.value
    assert run.input_gate is None
    assert run.input_request is None
    assert run.failure == "pr merged while parked"
    assert cancelled == [run.id]

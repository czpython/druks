import pytest
from druks.contrib.software_factory.workflows import Build
from druks.db import db_session
from druks.durable.enums import WorkflowEvent
from druks.events.models import Event
from druks.testing import seed_run
from druks_field_notes.models import Note
from druks_field_notes.workflows import Summarize
from software_factory.factories import make_test_work_item
from sqlalchemy import select


async def test_operator_stop_records_its_reason_and_exact_run_once(druks_client, druks_db):
    note = await Note.create(body="Stop this work")
    run = await seed_run(druks_db, kind=Summarize.kind, subject=note)

    response = await druks_client.post(f"/api/runs/{run.id}/cancel", json={"reason": "Wrong source"})
    assert response.status_code == 200
    assert response.json() == {"run": run.id, "result": "cancelled"}

    repeated = await druks_client.post(f"/api/runs/{run.id}/cancel", json={"reason": "Wrong source"})
    assert repeated.json() == {"run": run.id, "result": "already_cancelled"}
    events = list(await druks_db.scalars(select(Event).filter_by(type=WorkflowEvent.CANCELLED)))
    assert len(events) == 1
    assert events[0].payload == {"run": run.id, "kind": Summarize.kind, "reason": "Wrong source"}
    assert events[0].app == "field_notes"
    assert events[0].subject_id == str(note.id)
    assert events[0].subject_label == note.label


async def test_failed_operator_cancellation_records_no_stop(druks_client, druks_db, monkeypatch):
    note = await Note.create(body="Cancellation failed")
    run = await seed_run(druks_db, kind=Summarize.kind, subject=note)

    async def unavailable(workflow_id: str) -> None:
        raise RuntimeError("Cancellation unavailable")

    monkeypatch.setattr("dbos.DBOS.cancel_workflow_async", unavailable)
    with pytest.raises(RuntimeError, match="Cancellation unavailable"):
        await druks_client.post(f"/api/runs/{run.id}/cancel", json={"reason": "Wrong source"})

    assert not list(await druks_db.scalars(select(Event).filter_by(type=WorkflowEvent.CANCELLED)))


@pytest.mark.parametrize("state", ["failed", "finished"])
async def test_inactive_run_has_no_operator_stop(druks_client, druks_db, state):
    note = await Note.create(body="Finished work")
    run = await seed_run(druks_db, kind=Summarize.kind, subject=note, state=state)

    response = await druks_client.post(f"/api/runs/{run.id}/cancel", json={"reason": "Wrong source"})

    assert response.status_code == 409
    assert not list(await druks_db.scalars(select(Event).filter_by(type=WorkflowEvent.CANCELLED)))


@pytest.mark.parametrize("reason", ["Pull request merged", "Pull request closed"])
async def test_factory_cleanup_creates_no_operator_stop(druks_db, reason):
    db_session.registry.set(druks_db)
    item = await make_test_work_item(repo="owner/repo", title="Completed work")
    await seed_run(druks_db, kind=Build.kind, subject=item)

    await Build.cancel(item, failure=reason)

    assert not list(await druks_db.scalars(select(Event).filter_by(type=WorkflowEvent.CANCELLED)))

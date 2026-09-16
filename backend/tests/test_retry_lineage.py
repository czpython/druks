from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from conftest import make_test_note, seed_note_run
from druks.durable.engine import _step_engine
from druks.durable.models import Run
from druks.durable.reads import list_subject_timeline
from druks.testing import seed_dbos_status


@pytest.mark.parametrize("row_exists", [False, True])
@pytest.mark.parametrize("failed_step, reused", [(8, 1), (None, 0)])
async def test_retry_records_its_source_without_copying_calls(
    druks_db, monkeypatch, row_exists, failed_step, reused
):
    note = await make_test_note()
    source = await seed_note_run(druks_db, note=note, state="failed")
    steps = [{"function_id": 2, "error": None}]
    if failed_step:
        steps.extend(
            [
                {"function_id": 6, "error": RuntimeError("Handled failure")},
                {"function_id": failed_step, "error": RuntimeError("Final failure")},
            ]
        )
    monkeypatch.setattr("dbos.DBOS.list_workflow_steps_async", AsyncMock(return_value=steps))
    monkeypatch.setattr("druks.durable.models.publish", AsyncMock())

    async def fork(workflow_id, start_step, *, queue_name):
        await seed_dbos_status(druks_db, "retry", "scheduled", subject=note.identity)
        if row_exists:
            await Run.create_row(
                _step_engine(), workflow_id="retry", kind=source.kind, account_id=source.account_id
            )
        return SimpleNamespace(workflow_id="retry")

    monkeypatch.setattr("dbos.DBOS.fork_workflow_async", fork)

    await source.retry()
    await Run.create_row(
        _step_engine(), workflow_id="retry", kind=source.kind, account_id=source.account_id
    )
    druks_db.expunge_all()
    timeline = await list_subject_timeline(druks_db, "note", str(note.id))
    retried = next(run for run in timeline if run.id == "retry")
    original = next(run for run in timeline if run.id == source.id)

    assert retried.retry_from == source.id
    assert retried.retry_step == (failed_step or 1)
    assert retried.retry_reused_steps == reused
    assert retried.agent_calls == []
    assert original.retry_from is None
    assert original.retry_step is None

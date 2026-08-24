from druks.durable.dbos_state import workflow_status
from druks.durable.enums import AgentCallStatus
from druks.durable.models import AgentCall
from druks.testing import seed_call, seed_run
from druks_field_notes.models import Note
from druks_field_notes.workflows import Summarize
from sqlalchemy import update


async def _running_call(druks_db):
    note = await Note.create(body="agent call liveness")
    run = await seed_run(druks_db, kind=Summarize.kind, subject=note)
    return await seed_call(druks_db, run, "summarize", status="running")


async def test_an_unfinished_call_reads_running_while_its_run_is_active(druks_db):
    call = await AgentCall.get((await _running_call(druks_db)).id)
    assert call.live_status == AgentCallStatus.RUNNING


async def test_an_unfinished_call_reads_abandoned_once_its_run_is_terminal(druks_db):
    call = await _running_call(druks_db)
    await druks_db.execute(
        update(workflow_status)
        .where(workflow_status.c.workflow_uuid == call.run_id)
        .values(status="ERROR")
    )
    call = await AgentCall.get(call.id)
    assert call.live_status == AgentCallStatus.ABANDONED


async def test_a_finished_call_keeps_its_outcome(druks_db):
    note = await Note.create(body="finished agent call")
    run = await seed_run(druks_db, kind=Summarize.kind, subject=note)
    call = await seed_call(
        druks_db,
        run,
        "summarize",
        status=AgentCallStatus.SUCCEEDED.value,
    )
    # A finished call keeps its outcome regardless of its run's state.
    assert call.live_status == AgentCallStatus.SUCCEEDED

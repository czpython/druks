from druks.durable.dbos_state import workflow_status
from druks.durable.enums import AgentCallStatus
from druks.durable.schemas import AgentCallResponse
from druks.testing import seed_call, seed_run
from druks_field_notes.models import Note
from druks_field_notes.workflows import Summarize
from sqlalchemy import update


def _running_call(druks_db):
    note = Note.create(body="agent call liveness")
    run = seed_run(druks_db, kind=Summarize.kind, subject=note)
    return seed_call(druks_db, run, "summarize", status="running")


def test_from_call_derives_running_while_run_active(druks_db):
    call = _running_call(druks_db)
    assert AgentCallResponse.from_call(call).status == "running"


def test_from_call_derives_abandoned_when_run_terminal(druks_db):
    call = _running_call(druks_db)
    druks_db.execute(
        update(workflow_status)
        .where(workflow_status.c.workflow_uuid == call.run_id)
        .values(status="ERROR")
    )
    druks_db.expire_all()
    assert AgentCallResponse.from_call(call).status == "abandoned"


def test_from_call_keeps_a_finished_calls_outcome(druks_db):
    note = Note.create(body="finished agent call")
    run = seed_run(druks_db, kind=Summarize.kind, subject=note)
    call = seed_call(
        druks_db,
        run,
        "summarize",
        status=AgentCallStatus.SUCCEEDED.value,
    )
    # A finished call keeps its outcome regardless of its run's state.
    assert AgentCallResponse.from_call(call).status == "succeeded"

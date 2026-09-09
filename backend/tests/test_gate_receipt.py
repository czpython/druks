from unittest.mock import AsyncMock

import pytest
from dbos import DBOS
from dbos._error import DBOSWorkflowCancelledError
from druks.durable.exceptions import GateTimeout
from druks.durable.models import Run
from druks.events.models import Event
from druks.testing import seed_run
from druks.workflows import OperatorReply, current_workflow
from druks_field_notes.models import Note
from druks_field_notes.workflows import Summarize
from pydantic import ValidationError
from sqlalchemy import select

_ASK = {"presentation": "in_app", "controls": ["approve", "request_changes"], "questions": []}


@pytest.fixture
def direct_steps(monkeypatch):
    async def call_through(options, function, *args, **kwargs):
        return await function(*args, **kwargs)

    monkeypatch.setattr(DBOS, "run_step_async", call_through)
    monkeypatch.setattr("druks.workflows._notify_designated_destination", AsyncMock())


@pytest.fixture(params=["gate", "review"])
async def request_reply(request, druks_db, direct_steps):
    note = await Note.create(body="A request to review")
    run = await seed_run(druks_db, kind=Summarize.kind, subject=note)
    workflow = Summarize()
    workflow._workflow_id = run.id
    workflow._subject = note.identity
    token = current_workflow.set(workflow)
    try:
        call = (
            OperatorReply.wait(input_request=_ASK) if request.param == "gate" else workflow.review()
        )
        yield run.id, call
    finally:
        current_workflow.reset(token)


async def test_valid_reply_records_the_request_round_before_current_fields_clear(
    druks_db, request_reply, monkeypatch
):
    run_id, call = request_reply
    monkeypatch.setattr(DBOS, "recv_async", AsyncMock(return_value={"action": "approve"}))

    reply = await call

    assert reply == OperatorReply(action="approve")
    druks_db.expunge_all()
    run = await druks_db.get(Run, run_id)
    assert run.answer_parked_at == run.input_requested_at
    assert not run.input_gate
    assert not run.input_request
    events = list(await druks_db.scalars(select(Event).order_by(Event.id)))
    assert [event.type for event in events] == ["workflow.parked", "workflow.running"]
    request, receipt = events
    for event in events:
        assert event.payload["run"] == run_id
        assert event.payload["gate"] == "review"
        assert event.payload["input_requested_at"] == run.input_requested_at.isoformat()
    assert request.payload["input_request"] == _ASK
    assert receipt.payload["result"] == {"action": "approve", "answers": {}, "note": ""}


@pytest.mark.parametrize("payload", [{"action": "merge"}, {}, None])
async def test_invalid_reply_or_timeout_records_no_receipt(
    druks_db, request_reply, monkeypatch, payload
):
    run_id, call = request_reply
    monkeypatch.setattr(DBOS, "recv_async", AsyncMock(return_value=payload))

    with pytest.raises(GateTimeout if payload is None else ValidationError):
        await call

    druks_db.expunge_all()
    run = await druks_db.get(Run, run_id)
    assert not run.answer_parked_at
    assert run.input_gate == "review"
    assert run.input_request == _ASK
    assert run.input_requested_at
    events = list(await druks_db.scalars(select(Event)))
    assert [event.type for event in events] == ["workflow.parked"]


async def test_cancel_records_no_receipt(druks_db, request_reply, monkeypatch):
    run_id, call = request_reply
    monkeypatch.setattr(
        DBOS, "recv_async", AsyncMock(side_effect=DBOSWorkflowCancelledError(run_id))
    )

    with pytest.raises(DBOSWorkflowCancelledError):
        await call

    druks_db.expunge_all()
    run = await druks_db.get(Run, run_id)
    assert not run.answer_parked_at
    events = list(await druks_db.scalars(select(Event)))
    assert [event.type for event in events] == ["workflow.parked"]

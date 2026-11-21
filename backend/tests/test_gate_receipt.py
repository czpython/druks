from unittest.mock import AsyncMock

import pytest
from dbos import DBOS
from dbos._error import DBOSWorkflowCancelledError
from druks import ui
from druks.contrib.software_factory.workflows import PullRequestReview
from druks.durable.exceptions import GateTimeout
from druks.durable.models import Run
from druks.events.models import Event
from druks.testing import seed_run
from druks.ui.exceptions import NoTextFormError
from druks.workflows import OperatorReply, YesNo, current_workflow
from druks_field_notes.models import Note
from druks_field_notes.workflows import Summarize
from pydantic import ValidationError
from sqlalchemy import select

_ASK = {"presentation": "in_app", "controls": ["approve", "request_changes"], "questions": []}
_STORED_ASK = {
    **_ASK,
    "controls": [
        {"id": "approve", "note_required": False},
        {"id": "request_changes", "note_required": True},
    ],
    "reply_fields": ["action", "answers", "note"],
}
_NOTE = ui.Follows(subject_type="note", subject_id="7")


@pytest.fixture
def direct_steps(monkeypatch):
    async def call_through(options, function, *args, **kwargs):
        return await function(*args, **kwargs)

    monkeypatch.setattr(DBOS, "run_step_async", call_through)
    monkeypatch.setattr("druks.workflows._notify_account", AsyncMock())


@pytest.fixture
async def summarize(druks_db, direct_steps):
    """The proof app's workflow, in the body of its run on a note."""
    note = await Note.create(body="A plan")
    run = await seed_run(druks_db, kind=Summarize.kind, subject=note)
    workflow = Summarize()
    workflow._workflow_id = run.id
    workflow._subject = note.identity
    workflow.subject = note
    token = current_workflow.set(workflow)
    yield workflow
    current_workflow.reset(token)


@pytest.fixture(params=["gate", "review"])
async def request_reply(request, summarize):
    if request.param == "gate":
        call = OperatorReply.wait(input_request=_ASK)
    else:
        call = summarize.review()
    yield summarize.workflow_id, call


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
    assert request.payload["input_request"] == _STORED_ASK
    assert receipt.payload["result"] == {"action": "approve", "answers": {}, "note": ""}


async def test_yes_no_parks_with_its_own_controls(druks_db, summarize, monkeypatch):
    monkeypatch.setattr(DBOS, "recv_async", AsyncMock(return_value={"action": "yes", "note": "go"}))

    reply = await YesNo.wait(input_request={"presentation": "in_app", "label": "Keep it?"})

    assert reply == YesNo(action="yes", note="go")
    druks_db.expunge_all()
    parked = await druks_db.scalar(select(Event).where(Event.type == "workflow.parked"))
    assert parked.payload["input_request"] == {
        "presentation": "in_app",
        "label": "Keep it?",
        "questions": [],
        "controls": [
            {"id": "yes", "note_required": False},
            {"id": "no", "note_required": False},
        ],
        "reply_fields": ["action", "note"],
    }


@pytest.mark.parametrize(
    "context, stored, note_required",
    [
        ("Keep the scope small.", "Keep the scope small.", False),
        ("   ", "   ", True),
        (
            [ui.Facts([ui.Fact("Work", ui.SubjectStatus(_NOTE))], title="The note")],
            [
                {
                    "block": "facts",
                    "title": "The note",
                    "facts": [
                        {
                            "label": "Work",
                            "value": {
                                "value": "subject_status",
                                "subject": {"subjectType": "note", "subjectId": "7"},
                                "working": "working",
                            },
                        }
                    ],
                }
            ],
            False,
        ),
        ([], None, True),
    ],
    ids=["text", "blank text", "blocks", "no blocks"],
)
async def test_the_ask_keeps_its_context_and_a_guiding_one_spares_request_changes_a_note(
    druks_db, summarize, monkeypatch, context, stored, note_required
):
    asks = []

    async def answer(*args, **kwargs):
        parked = await druks_db.get(Run, summarize.workflow_id, populate_existing=True)
        asks.append(parked.input_request)
        return {"action": "approve"}

    monkeypatch.setattr(DBOS, "recv_async", answer)

    await summarize.review(context=context)

    druks_db.expunge_all()
    parked = await druks_db.scalar(select(Event).where(Event.type == "workflow.parked"))
    for ask in [*asks, parked.payload["input_request"]]:
        assert ask.get("context") == stored
        assert ask["controls"] == [
            {"id": "approve", "note_required": False},
            {"id": "request_changes", "note_required": note_required},
        ]


async def test_a_block_context_reads_where_the_work_stands_each_time(
    druks_db, summarize, monkeypatch
):
    note = await summarize.subject
    other = await Note.create(body="Another plan")
    asks = []
    stored = []

    async def answer(*args, **kwargs):
        parked = await druks_db.get(Run, summarize.workflow_id, populate_existing=True)
        asks.append(await parked.get_ask())
        # Another app works on the other note, and its run is not this app's work.
        await seed_run(druks_db, kind=PullRequestReview.kind, subject=other, state="running")
        asks.append(await parked.get_ask())
        await seed_run(druks_db, kind=Summarize.kind, subject=other, state="failed")
        asks.append(await parked.get_ask())
        stored.append(parked.input_request)
        return {"action": "approve"}

    monkeypatch.setattr(DBOS, "recv_async", answer)

    await summarize.review(
        context=[
            ui.Facts(
                [ui.Fact("This", ui.SubjectStatus(note)), ui.Fact("That", ui.SubjectStatus(other))]
            )
        ]
    )

    statuses = [[fact["value"]["status"] for fact in ask["context"][0]["facts"]] for ask in asks]
    assert [(this["state"], this["gate"]) for this, _ in statuses] == [("parked", "review")] * 3
    assert [that["state"] for _, that in statuses] == [None, None, "failed"]
    assert "status" not in stored[0]["context"][0]["facts"][0]["value"]


@pytest.mark.parametrize(
    "context, refused",
    [
        (
            [ui.Section(blocks=[ui.GateControls(_NOTE)])],
            "GateControls",
        ),
        (
            [
                ui.Callout(
                    "Retry it.",
                    controls=[ui.Action(label="Retry", operation="write_note", refresh="region")],
                )
            ],
            "Action",
        ),
        (
            [
                ui.Table(
                    columns=[ui.TableColumn("Do")],
                    rows=[
                        ui.TableRow(
                            [ui.ControlsValue([ui.Action(label="Go", operation="write_note")])]
                        )
                    ],
                )
            ],
            "Action",
        ),
        ([ui.Cards(drop=ui.Action(label="Move", operation="write_note"))], "Action"),
        ([ui.Stack([ui.Form(action=ui.Action(label="Save", operation="write_note"))])], "Form"),
    ],
    ids=["gate controls", "action in a region it lacks", "table cell", "drop", "form"],
)
async def test_review_refuses_a_part_with_no_text_form_before_it_parks(
    druks_db, summarize, context, refused
):
    with pytest.raises(NoTextFormError) as raised:
        await summarize.review(context=context)

    assert raised.value.block == refused
    assert not list(await druks_db.scalars(select(Event)))


async def test_review_refuses_a_part_that_is_not_a_block_before_it_parks(druks_db, summarize):
    with pytest.raises(ValidationError):
        await summarize.review(context=[ui.Fact("Owner", "Ada")])

    assert not list(await druks_db.scalars(select(Event)))


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
    assert run.input_request == _STORED_ASK
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

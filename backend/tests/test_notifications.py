from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from conftest import FORM_TEXT
from druks.accounts.models import Account
from druks.chat.bots.service import request_approval
from druks.chat.constants import CLICK_ANSWER_MESSAGE
from druks.chat.enums import ConversationSource, MessageRole
from druks.chat.forms import parse_fields
from druks.chat.models import Conversation, Message
from druks.chat.service import deliver
from druks.contrib.software_factory.workflows import Build
from druks.durable import Run
from druks.models import Base
from druks.notifications.datastructures import NotificationState
from druks.notifications.exceptions import (
    AlreadyAcknowledgedError,
    AnswerNotAllowedError,
    InvalidChoiceError,
)
from druks.notifications.models import Notification
from druks.notifications.services import respond_to_notification
from druks.teams.models import Team
from druks.testing import configure_app_for_test, make_settings, seed_dbos_status, seed_run
from druks_field_notes.workflows import Summarize
from fastapi.testclient import TestClient
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from uuid_utils import uuid7

_SUBJECT = {"type": "work_item", "id": 1}


async def test_unique_token_collision_raises(druks_db):
    account = await Account.get_or_create(druks_db, "op@example.com")
    first = await Notification.create(
        druks_db, account_id=account.id, reason="r", body="b", subject=_SUBJECT
    )

    duplicate = Notification(
        account_id=account.id,
        reason="r",
        body="b",
        subject=_SUBJECT,
        correlation_token=first.correlation_token,
    )
    druks_db.add(duplicate)
    with pytest.raises(IntegrityError):
        await druks_db.flush()


async def test_only_its_person_reads_a_notification(tmp_path, druks_db):
    owner = await Account.get_or_create(druks_db, "owner@example.com")
    await Account.get_or_create(druks_db, "other@example.com")
    conversation = await Conversation.create(druks_db, account_id=owner.id, body="Which one?")
    notifications = [
        await Notification.create(druks_db, account_id=owner.id, reason="r", body="b"),
        await Notification.create(
            druks_db, conversation_id=conversation.id, reason="question.asked", body="b"
        ),
    ]
    await druks_db.commit()
    settings = make_settings(tmp_path, identity={"mode": "header", "header": "X-ExeDev-Email"})
    app = configure_app_for_test(settings=settings, authenticated=False)

    with TestClient(app) as client:
        for notification in notifications:
            path = f"/api/notifications/{notification.id}"
            as_owner = client.get(path, headers={"X-ExeDev-Email": "owner@example.com"})
            as_other = client.get(path, headers={"X-ExeDev-Email": "other@example.com"})
            assert (as_owner.status_code, as_other.status_code) == (200, 404)


async def test_the_list_holds_only_the_person_s_own_notifications(tmp_path, druks_db):
    owner = await Account.get_or_create(druks_db, "owner@example.com")
    other = await Account.get_or_create(druks_db, "other@example.com")
    mine = await Notification.create(druks_db, account_id=owner.id, reason="r", body="b")
    await Notification.create(druks_db, account_id=other.id, reason="r", body="b")
    await druks_db.commit()
    settings = make_settings(tmp_path, identity={"mode": "header", "header": "X-ExeDev-Email"})
    app = configure_app_for_test(settings=settings, authenticated=False)

    with TestClient(app) as client:
        response = client.get("/api/notifications", headers={"X-ExeDev-Email": "owner@example.com"})
    assert [notification["id"] for notification in response.json()] == [mine.id]


async def test_a_person_marks_only_their_own_notifications_read(tmp_path, druks_db):
    owner = await Account.get_or_create(druks_db, "owner@example.com")
    other = await Account.get_or_create(druks_db, "other@example.com")
    mine = await Notification.create(druks_db, account_id=owner.id, reason="r", body="b")
    theirs = await Notification.create(druks_db, account_id=other.id, reason="r", body="b")
    await druks_db.commit()
    settings = make_settings(tmp_path, identity={"mode": "header", "header": "X-ExeDev-Email"})
    app = configure_app_for_test(settings=settings, authenticated=False)

    with TestClient(app) as client:
        response = client.post(
            "/api/notifications/read", headers={"X-ExeDev-Email": "owner@example.com"}
        )
    assert response.status_code == 204
    await druks_db.refresh(mine)
    await druks_db.refresh(theirs)
    assert mine.read_at is not None
    assert theirs.read_at is None


async def test_endpoints_list_and_get_omit_the_token(tmp_path, druks_db):
    owner = await Account.get_or_create(druks_db, "owner@example.com")
    tokens = []
    for index in range(3):
        notification = await Notification.create(
            druks_db, account_id=owner.id, reason="gate.parked", body=f"note {index}"
        )
        tokens.append(notification.correlation_token)
    failed = await Notification.create(
        druks_db, account_id=owner.id, reason="gate.parked", body="bad"
    )
    await failed.mark_failed("DeliveryError: HTTPStatusError")
    tokens.append(failed.correlation_token)
    await druks_db.commit()
    settings = make_settings(tmp_path, identity={"mode": "header", "header": "X-ExeDev-Email"})
    app = configure_app_for_test(settings=settings, authenticated=False)

    with TestClient(app, headers={"X-ExeDev-Email": "owner@example.com"}) as client:
        listed = client.get("/api/notifications", params={"limit": 2})
        assert listed.status_code == 200
        items = listed.json()
        assert len(items) == 2
        assert items[0]["body"] == "bad"
        assert items[0]["state"] == "failed"
        assert items[0]["lastError"] == "DeliveryError: HTTPStatusError"
        for item in items:
            assert "correlationToken" not in item
        for token in tokens:
            assert token not in listed.text

        one = client.get(f"/api/notifications/{failed.id}")
        assert one.status_code == 200
        assert one.json()["id"] == failed.id
        assert one.json()["attempts"] == 0
        assert failed.correlation_token not in one.text

        assert client.get("/api/notifications/no-such-id").status_code == 404


async def test_a_member_outside_the_apps_team_reads_none_of_its_notifications(tmp_path, druks_db):
    await Account.get_or_create(druks_db, "admin@example.com")
    member = await Account.get_or_create(druks_db, "op@example.com")
    await Team.create(druks_db, name="Notes", member_ids=[], apps=["field_notes"])
    notifications = {}
    for kind in (Summarize.kind, Build.kind):
        run = await seed_run(druks_db, kind=kind)
        notifications[kind] = await Notification.create(
            druks_db, account_id=member.id, reason="gate.parked", body=kind, run_id=run.id
        )
    runless = await Notification.create(
        druks_db, account_id=member.id, reason="subscription.disconnected", body="no run"
    )

    with TestClient(configure_app_for_test(settings=make_settings(tmp_path))) as client:
        listed = client.get("/api/notifications").json()
        hidden = client.get(f"/api/notifications/{notifications[Summarize.kind].id}")
        shown = client.get(f"/api/notifications/{notifications[Build.kind].id}")

    assert [item["id"] for item in listed] == [runless.id, notifications[Build.kind].id]
    assert hidden.status_code == 403
    assert shown.status_code == 200


_IN_APP_ASK = {
    "presentation": "in_app",
    "controls": [
        {"id": "approve", "note_required": False},
        {"id": "request_changes", "note_required": True},
    ],
    "questions": [{"id": "q1", "prompt": "which?", "options": [{"id": "a", "label": "A"}]}],
}


async def _parked_notification(druks_db, *, ask=None, run_state="parked"):
    ask = ask or _IN_APP_ASK
    run = Run(
        account_id=(await Account.get_or_create(druks_db, "op@example.com")).id,
        id=str(uuid7()),
        kind=Summarize.kind,
        input_gate="review",
        input_request=ask,
        input_requested_at=Base.utc_now(),
    )
    druks_db.add(run)
    await druks_db.flush()
    await seed_dbos_status(druks_db, run.id, run_state)
    notification = await Notification.create(
        druks_db,
        account_id=run.account_id,
        reason="gate.parked",
        body="review the plan",
        subject={"type": "notification_probe", "id": 1},
        run_id=run.id,
        run_parked_at=run.input_requested_at,
    )
    return run, notification


async def test_a_control_that_needs_a_note_gets_a_button_only_in_a_chat(druks_db):
    run, _ = await _parked_notification(druks_db)
    approve = {"id": "approve", "label": "Approve"}
    request_changes = {"id": "request_changes", "label": "Request changes"}

    assert (await run.get_rendered_ask(has_chat=False))["actions"] == [approve]
    assert (await run.get_rendered_ask(has_chat=True))["actions"] == [approve, request_changes]

    run.input_request = {
        **_IN_APP_ASK,
        "controls": [
            {"id": "approve", "note_required": False},
            {"id": "request_changes", "note_required": False},
        ],
    }
    assert (await run.get_rendered_ask(has_chat=False))["actions"] == [approve, request_changes]


async def _thread_notification(druks_db, *, ask):
    account = await Account.get_or_create(druks_db, "op@example.com")
    conversation = Conversation(
        account_id=account.id,
        source=ConversationSource.SLACK,
        user_id="U1",
        thread_id="C9:1700000000.000100",
    )
    druks_db.add(conversation)
    await druks_db.flush()
    run = Run(
        account_id=account.id,
        id=str(uuid7()),
        kind=Summarize.kind,
        input_gate="review",
        input_request=ask,
        input_requested_at=Base.utc_now(),
        conversation_id=conversation.id,
    )
    druks_db.add(run)
    await druks_db.flush()
    await seed_dbos_status(druks_db, run.id, "parked")
    notification_id = await run.create_park_notification(
        {"type": "notification_probe", "id": 1}, conversation_id=conversation.id
    )
    return run, await druks_db.get(Notification, notification_id)


@pytest.fixture
def resume_spy(monkeypatch):
    calls = []

    async def _spy(self, **fields):
        calls.append({"id": self.id, **fields})

    monkeypatch.setattr(Run, "resume", _spy)
    return calls


async def test_respond_resumes_and_marks_acknowledged(druks_db, resume_spy):
    run, notification = await _parked_notification(druks_db)

    await respond_to_notification(
        druks_db,
        notification.correlation_token,
        {"control": "approve", "answers": {"q1": "a"}, "note": ""},
        user_id=None,
        account_id=None,
    )

    assert resume_spy == [{"id": run.id, "action": "approve", "answers": {"q1": "a"}, "note": ""}]
    assert (await druks_db.get(Notification, notification.id)).state == "acknowledged"
    assert (await druks_db.get(Notification, notification.id)).is_acknowledged


async def test_only_the_chat_s_person_answers_in_its_thread(druks_db, resume_spy):
    _, notification = await _thread_notification(druks_db, ask={**_IN_APP_ASK, "questions": []})

    with pytest.raises(AnswerNotAllowedError):
        await respond_to_notification(
            druks_db,
            notification.correlation_token,
            {"control": "approve"},
            user_id="U2",
            account_id=None,
        )

    assert resume_spy == []
    assert not notification.is_acknowledged


async def test_a_click_that_answers_in_a_thread_tells_the_chat(druks_db, resume_spy):
    run, notification = await _thread_notification(druks_db, ask={**_IN_APP_ASK, "questions": []})

    await respond_to_notification(
        druks_db,
        notification.correlation_token,
        {"control": "approve"},
        user_id="U1",
        account_id=None,
    )

    assert resume_spy == [{"id": run.id, "action": "approve", "answers": {}, "note": ""}]
    internal = await druks_db.scalars(
        select(Message.body).where(
            Message.conversation_id == run.conversation_id, Message.is_internal
        )
    )
    assert list(internal) == [CLICK_ANSWER_MESSAGE.format(run=run.id, answer="Approve")]


@pytest.mark.parametrize(
    ("ask", "control", "label"),
    [
        ({**_IN_APP_ASK, "questions": []}, "request_changes", "Request changes"),
        (_IN_APP_ASK, "approve", "Approve"),
    ],
)
async def test_a_click_that_needs_more_goes_to_the_chat(
    druks_db, resume_spy, monkeypatch, ask, control, label
):
    started = AsyncMock()
    monkeypatch.setattr(
        "druks.notifications.services.DBOS", SimpleNamespace(start_workflow_async=started)
    )
    run, notification = await _thread_notification(druks_db, ask=ask)
    await request_approval(druks_db, run)

    await respond_to_notification(
        druks_db,
        notification.correlation_token,
        {"control": control},
        user_id="U1",
        account_id=None,
    )

    assert resume_spy == []
    assert notification.is_acknowledged
    conversation = await druks_db.get(Conversation, run.conversation_id)
    click = await conversation.get_unanswered_message(druks_db)
    assert (click.body, click.role, click.is_internal) == (label, MessageRole.USER, False)
    started.assert_awaited_once_with(deliver, run.conversation_id)


CHOICES = {"actions": [{"id": "1", "label": "Postgres"}, {"id": "2", "label": "MySQL"}]}
FORM = {"fields": parse_fields(FORM_TEXT)}
ANSWERS = {
    "title": " Fix the build ",
    "notes": "",
    "budget": "15",
    "where": "production",
    "tags": ["shelf", "rack"],
    "size": "l",
    "notify": False,
}


async def _question_notification(druks_db, *, person="U1", ask=CHOICES):
    """A question that the agent of a Slack chat with ``person`` asked, as its turn left
    it: with choices, or with the fields of a form."""
    account = await Account.get_or_create(druks_db, "op@example.com")
    conversation = Conversation(
        account_id=account.id,
        source=ConversationSource.SLACK,
        user_id=person,
        thread_id="C9:1700000000.000100",
    )
    druks_db.add(conversation)
    await druks_db.flush()
    return await Notification.create(
        druks_db,
        conversation_id=conversation.id,
        reason="question.asked",
        body="Which database?",
        question_offset=0,
        **ask,
    )


async def _list_messages(druks_db, question) -> list:
    """The messages of the question's chat: body, whether Druks wrote it, and state."""
    messages = await druks_db.execute(
        select(Message.body, Message.is_internal, Message.state).where(
            Message.conversation_id == question.conversation_id
        )
    )
    return messages.all()


@pytest.fixture
def started(monkeypatch):
    """The chat deliveries that a click starts."""
    started = AsyncMock()
    monkeypatch.setattr(
        "druks.notifications.services.DBOS", SimpleNamespace(start_workflow_async=started)
    )
    return started


async def test_a_click_on_a_chat_question_is_the_person_s_next_message(druks_db, started):
    question = await _question_notification(druks_db)

    await respond_to_notification(
        druks_db, question.correlation_token, {"control": "2"}, user_id="U1", account_id=None
    )

    assert question.is_acknowledged
    messages = await druks_db.execute(
        select(Message.body, Message.is_internal, Message.state).where(
            Message.conversation_id == question.conversation_id
        )
    )
    assert messages.all() == [("MySQL", False, "pending")]
    started.assert_awaited_once_with(deliver, question.conversation_id)


async def test_a_link_answers_no_chat_question(druks_db, started):
    # A link names nobody, so it answers no chat, even one whose person is unknown.
    question = await _question_notification(druks_db, person=None)

    with pytest.raises(AnswerNotAllowedError):
        await respond_to_notification(
            druks_db, question.correlation_token, {"control": "1"}, user_id=None, account_id=None
        )

    assert not question.is_acknowledged
    started.assert_not_awaited()


async def test_a_chat_without_a_run_takes_only_a_choice_that_its_question_offered(
    druks_db, started
):
    question = await _question_notification(druks_db)
    notice = await Notification.create(
        druks_db, conversation_id=question.conversation_id, reason="r", body="No buttons."
    )

    with pytest.raises(InvalidChoiceError, match="unknown control '9'"):
        await respond_to_notification(
            druks_db, question.correlation_token, {"control": "9"}, user_id="U1", account_id=None
        )
    with pytest.raises(InvalidChoiceError):
        await respond_to_notification(
            druks_db, notice.correlation_token, {"control": "1"}, user_id="U1", account_id=None
        )

    assert not question.is_acknowledged
    started.assert_not_awaited()


async def _submit(druks_db, question, values, *, person="U1"):
    """The Submit of the form ``question`` by ``person``, with ``values`` in its inputs."""
    return await respond_to_notification(
        druks_db,
        question.correlation_token,
        {"control": "submit"},
        user_id=person,
        account_id=None,
        values=values,
    )


@pytest.mark.parametrize(
    ("values", "body"),
    [
        (
            ANSWERS,
            "Title: Fix the build\nNotes: (no answer)\nBudget: 15\nWhere: Production\n"
            "Tags: Rack, Shelf\nSize: Large\nNotify the owner: No",
        ),
        (
            {"title": "Fix it", "size": "s", "notify": True},
            "Title: Fix it\nNotes: (no answer)\nBudget: (no answer)\nWhere: (no answer)\n"
            "Tags: (no answer)\nSize: Small\nNotify the owner: Yes",
        ),
    ],
    ids=["every-field", "the-required-fields"],
)
async def test_a_submit_by_the_person_is_their_next_message_with_a_line_for_each_field(
    druks_db, started, values, body
):
    question = await _question_notification(druks_db, ask=FORM)

    await _submit(druks_db, question, values)

    assert question.is_acknowledged
    assert await _list_messages(druks_db, question) == [(body, False, "pending")]
    started.assert_awaited_once_with(deliver, question.conversation_id)


BUDGET_REFUSAL = 'Write a number in "Budget". The minimum is 0. The maximum is 100. The step is 5.'


@pytest.mark.parametrize(
    ("fields", "values", "reason"),
    [
        (FORM_TEXT, {**ANSWERS, "title": "  "}, 'Answer "Title".'),
        (FORM_TEXT, {**ANSWERS, "size": ""}, 'Answer "Size".'),
        (FORM_TEXT, {**ANSWERS, "budget": "a lot"}, BUDGET_REFUSAL),
        (FORM_TEXT, {**ANSWERS, "budget": "nan"}, BUDGET_REFUSAL),
        (FORM_TEXT, {**ANSWERS, "budget": "105"}, BUDGET_REFUSAL),
        (FORM_TEXT, {**ANSWERS, "budget": "12"}, BUDGET_REFUSAL),
        (FORM_TEXT, {**ANSWERS, "where": "moon"}, 'Choose "Where" from its options.'),
        (FORM_TEXT, {**ANSWERS, "tags": ["shelf", "moon"]}, 'Choose "Tags" from its options.'),
        (
            '[CheckboxField(name="terms", label="I agree", is_required=True)]',
            {"terms": False},
            'Check "I agree".',
        ),
    ],
    ids=[
        "blank-text",
        "no-choice",
        "not-a-number",
        "not-a-finite-number",
        "above-the-maximum",
        "off-the-step",
        "unknown-option",
        "unknown-options",
        "unchecked-box",
    ],
)
async def test_a_submit_that_its_fields_refuse_leaves_the_form_open(
    druks_db, started, fields, values, reason
):
    question = await _question_notification(druks_db, ask={"fields": parse_fields(fields)})

    with pytest.raises(InvalidChoiceError) as refused:
        await _submit(druks_db, question, values)

    assert str(refused.value) == reason
    assert not question.is_acknowledged
    assert await _list_messages(druks_db, question) == []
    started.assert_not_awaited()


async def test_a_person_corrects_a_refused_submit_and_submits_again(druks_db, started):
    question = await _question_notification(druks_db, ask=FORM)

    with pytest.raises(InvalidChoiceError):
        await _submit(druks_db, question, {**ANSWERS, "budget": "a lot"})
    await _submit(druks_db, question, ANSWERS)

    assert question.is_acknowledged
    assert len(await _list_messages(druks_db, question)) == 1
    started.assert_awaited_once_with(deliver, question.conversation_id)


async def test_a_submit_by_another_person_answers_nothing(druks_db, started):
    question = await _question_notification(druks_db, ask=FORM)

    with pytest.raises(AnswerNotAllowedError):
        await _submit(druks_db, question, ANSWERS, person="U2")

    assert not question.is_acknowledged
    assert await _list_messages(druks_db, question) == []
    started.assert_not_awaited()


@pytest.mark.parametrize("is_seen", [True, False], ids=["closed-before", "claimed-meanwhile"])
async def test_a_submit_after_another_answer_creates_no_message(druks_db, started, is_seen):
    question = await _question_notification(druks_db, ask=FORM)
    # Another answer took the form, before this Submit read it or while it checked it.
    await druks_db.execute(
        update(Notification)
        .where(Notification.id == question.id)
        .values(state=NotificationState.ACKNOWLEDGED.value),
        execution_options={"synchronize_session": "evaluate" if is_seen else False},
    )

    with pytest.raises(AlreadyAcknowledgedError):
        await _submit(druks_db, question, ANSWERS)

    assert await _list_messages(druks_db, question) == []
    started.assert_not_awaited()


async def test_the_respond_route_answers_no_form(tmp_path, druks_db, started):
    question = await _question_notification(druks_db, ask=FORM)
    client = TestClient(configure_app_for_test(settings=make_settings(tmp_path)))

    response = client.post(
        f"/_external/notifications/{question.correlation_token}/respond",
        json={"control": "submit", "answers": {"title": "Fix the build"}},
    )

    assert response.status_code == 403
    assert (await druks_db.get(Notification, question.id, populate_existing=True)).state == (
        "pending"
    )
    assert await _list_messages(druks_db, question) == []
    started.assert_not_awaited()


async def test_respond_route_codes_and_secret_hygiene(tmp_path, druks_db, resume_spy):
    run, notification = await _parked_notification(druks_db)
    token = notification.correlation_token
    client = TestClient(configure_app_for_test(settings=make_settings(tmp_path)))

    unknown = client.post(
        "/_external/notifications/no-such-token/respond", json={"control": "approve"}
    )
    assert unknown.status_code == 404
    assert "no-such-token" not in unknown.json()["detail"]

    bad_control = client.post(
        f"/_external/notifications/{token}/respond", json={"control": "merge"}
    )
    assert bad_control.status_code == 422

    bad_question = client.post(
        f"/_external/notifications/{token}/respond",
        json={"control": "approve", "answers": {"q9": "x"}},
    )
    assert bad_question.status_code == 422

    blank_answer = client.post(
        f"/_external/notifications/{token}/respond",
        json={"control": "approve", "answers": {"q1": "   "}},
    )
    assert blank_answer.status_code == 422

    empty_changes = client.post(
        f"/_external/notifications/{token}/respond", json={"control": "request_changes"}
    )
    assert empty_changes.status_code == 422
    assert resume_spy == []

    ok = client.post(f"/_external/notifications/{token}/respond", json={"control": "approve"})
    assert ok.status_code == 204
    assert len(resume_spy) == 1

    again = client.post(f"/_external/notifications/{token}/respond", json={"control": "approve"})
    assert again.status_code == 409
    assert len(resume_spy) == 1
    for response in (unknown, bad_control, bad_question, blank_answer, empty_changes, again):
        assert token not in response.text


async def test_respond_external_notification_not_answerable(tmp_path, druks_db, resume_spy):
    run, notification = await _parked_notification(
        druks_db, ask={"presentation": "external", "label": "Answer on the ticket"}
    )
    client = TestClient(configure_app_for_test(settings=make_settings(tmp_path)))

    response = client.post(
        f"/_external/notifications/{notification.correlation_token}/respond",
        json={"control": "approve"},
    )

    assert response.status_code == 422
    assert "informational" in response.json()["detail"]
    assert resume_spy == []


async def test_respond_runless_notification_not_answerable(tmp_path, druks_db, resume_spy):
    account = await Account.get_or_create(druks_db, "op@example.com")
    notification = await Notification.create(
        druks_db, account_id=account.id, reason="r", body="b", subject=_SUBJECT
    )
    client = TestClient(configure_app_for_test(settings=make_settings(tmp_path)))

    response = client.post(
        f"/_external/notifications/{notification.correlation_token}/respond",
        json={"control": "approve"},
    )

    assert response.status_code == 422
    assert resume_spy == []


async def test_respond_stale_round_409(tmp_path, druks_db, resume_spy):
    run = Run(
        account_id=(await Account.get_or_create(druks_db, "op@example.com")).id,
        id=str(uuid7()),
        kind=Summarize.kind,
        input_gate="review",
        input_request=_IN_APP_ASK,
        input_requested_at=Base.utc_now(),
    )
    druks_db.add(run)
    await druks_db.flush()
    await seed_dbos_status(druks_db, run.id, "parked")
    notification = await Notification.create(
        druks_db,
        account_id=run.account_id,
        reason="gate.parked",
        body="review",
        subject=_SUBJECT,
        run_id=run.id,
        run_parked_at=run.input_requested_at - timedelta(minutes=1),
    )
    client = TestClient(configure_app_for_test(settings=make_settings(tmp_path)))

    response = client.post(
        f"/_external/notifications/{notification.correlation_token}/respond",
        json={"control": "approve"},
    )

    assert response.status_code == 409
    assert resume_spy == []


async def test_respond_run_no_longer_parked_409(tmp_path, druks_db, resume_spy):
    run, notification = await _parked_notification(druks_db, run_state="finished")
    client = TestClient(configure_app_for_test(settings=make_settings(tmp_path)))

    response = client.post(
        f"/_external/notifications/{notification.correlation_token}/respond",
        json={"control": "approve"},
    )

    assert response.status_code == 409
    assert resume_spy == []


async def test_respond_direct_call_rejects_whitespace_only_content(druks_db, resume_spy):
    # The core is also the direct-call boundary (the Slack rail bypasses the
    # HTTP models' whitespace stripping) — blank means blank on every path.
    run, notification = await _parked_notification(druks_db)

    with pytest.raises(InvalidChoiceError):
        await respond_to_notification(
            druks_db,
            notification.correlation_token,
            {"control": "approve", "answers": {"q1": "   "}},
            user_id=None,
            account_id=None,
        )
    with pytest.raises(InvalidChoiceError):
        await respond_to_notification(
            druks_db,
            notification.correlation_token,
            {"control": "request_changes", "note": "   "},
            user_id=None,
            account_id=None,
        )
    assert resume_spy == []
    assert (await druks_db.get(Notification, notification.id)).state == "pending"


async def test_respond_ask_without_presentation_not_answerable(tmp_path, druks_db, resume_spy):
    # An ask that doesn't declare in_app isn't answerable via this rail — the
    # mapped 422, not a crash.
    run, notification = await _parked_notification(
        druks_db, ask={"label": "legacy ask", "controls": ["approve"]}
    )
    client = TestClient(configure_app_for_test(settings=make_settings(tmp_path)))

    response = client.post(
        f"/_external/notifications/{notification.correlation_token}/respond",
        json={"control": "approve"},
    )

    assert response.status_code == 422
    assert resume_spy == []


async def test_a_link_answers_only_a_run_of_an_app_that_no_team_lists(
    tmp_path, druks_db, resume_spy
):
    open_run, open_notification = await _parked_notification(druks_db)
    client = TestClient(configure_app_for_test(settings=make_settings(tmp_path)))
    opened = client.post(
        f"/_external/notifications/{open_notification.correlation_token}/respond",
        json={"control": "approve"},
    )
    await Team.create(druks_db, name="Notes", member_ids=[], apps=["field_notes"])
    _, listed_notification = await _parked_notification(druks_db)
    refused = client.post(
        f"/_external/notifications/{listed_notification.correlation_token}/respond",
        json={"control": "approve"},
    )

    assert opened.status_code == 204
    assert refused.status_code == 403
    assert [call["id"] for call in resume_spy] == [open_run.id]
    assert not (await druks_db.get(Notification, listed_notification.id)).is_acknowledged


async def test_a_run_of_an_app_that_is_not_installed_takes_no_answer(
    tmp_path, druks_db, resume_spy
):
    run, notification = await _parked_notification(druks_db)
    run.kind = "removed_app.work"
    await druks_db.flush()
    client = TestClient(configure_app_for_test(settings=make_settings(tmp_path)))

    response = client.post(
        f"/_external/notifications/{notification.correlation_token}/respond",
        json={"control": "approve"},
    )

    assert response.status_code == 404
    assert resume_spy == []

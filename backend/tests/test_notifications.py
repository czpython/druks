from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from druks.accounts.models import Account
from druks.chat.constants import CLICK_ANSWER_MESSAGE
from druks.chat.enums import ConversationSource
from druks.chat.models import Conversation, Message
from druks.chat.service import deliver
from druks.db import db_session
from druks.durable import Run
from druks.models import Base
from druks.notifications.exceptions import AnswerNotAllowedError, InvalidChoiceError
from druks.notifications.models import Destination, Notification
from druks.notifications.services import respond_to_notification
from druks.testing import configure_app_for_test, make_settings, seed_dbos_status
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from uuid_utils import uuid7

_SUBJECT = {"type": "work_item", "id": 1}


async def _destination(name: str = "ops") -> Destination:
    return await Destination.create(db_session(), name=name, kind="slack_channel", channel_id="C1")


async def test_unique_token_collision_raises(druks_db):
    destination = await _destination()
    first = await Notification.create(
        druks_db, destination_id=destination.id, reason="r", body="b", subject=_SUBJECT
    )

    duplicate = Notification(
        destination_id=destination.id,
        reason="r",
        body="b",
        subject=_SUBJECT,
        correlation_token=first.correlation_token,
    )
    druks_db.add(duplicate)
    with pytest.raises(IntegrityError):
        await druks_db.flush()


async def test_list_recent_newest_first_with_limit(druks_db):
    destination = await _destination()
    ids = [
        (
            await Notification.create(
                druks_db,
                destination_id=destination.id,
                reason="r",
                body=f"note {i}",
                subject=_SUBJECT,
            )
        ).id
        for i in range(3)
    ]

    listed = await Notification.list_recent(druks_db, limit=2)
    assert [notification.id for notification in listed] == [ids[2], ids[1]]


async def test_list_recent_leaves_out_a_person_s_notice(druks_db):
    destination = await _destination()
    account = await Account.get_or_create(druks_db, "op@example.com")
    shared = await Notification.create(
        druks_db, destination_id=destination.id, reason="r", body="b", subject=_SUBJECT
    )
    await Notification.create(druks_db, account_id=account.id, reason="r", body="b")

    listed = await Notification.list_recent(druks_db)
    assert [notification.id for notification in listed] == [shared.id]


async def test_only_its_person_reads_a_person_s_notice(tmp_path, druks_db):
    owner = await Account.get_or_create(druks_db, "owner@example.com")
    await Account.get_or_create(druks_db, "other@example.com")
    notification = await Notification.create(druks_db, account_id=owner.id, reason="r", body="b")
    await druks_db.commit()
    settings = make_settings(tmp_path, identity={"mode": "header", "header": "X-ExeDev-Email"})
    app = configure_app_for_test(settings=settings, authenticated=False)

    with TestClient(app) as client:
        path = f"/api/notifications/{notification.id}"
        as_owner = client.get(path, headers={"X-ExeDev-Email": "owner@example.com"})
        as_other = client.get(path, headers={"X-ExeDev-Email": "other@example.com"})
    assert as_owner.status_code == 200
    assert as_other.status_code == 404


async def test_the_inbox_lists_only_the_person_s_own_notices(tmp_path, druks_db):
    owner = await Account.get_or_create(druks_db, "owner@example.com")
    other = await Account.get_or_create(druks_db, "other@example.com")
    destination = await _destination()
    mine = await Notification.create(druks_db, account_id=owner.id, reason="r", body="b")
    await Notification.create(druks_db, account_id=other.id, reason="r", body="b")
    await Notification.create(
        druks_db, destination_id=destination.id, reason="r", body="b", subject=_SUBJECT
    )
    await druks_db.commit()
    settings = make_settings(tmp_path, identity={"mode": "header", "header": "X-ExeDev-Email"})
    app = configure_app_for_test(settings=settings, authenticated=False)

    with TestClient(app) as client:
        response = client.get(
            "/api/notifications/inbox", headers={"X-ExeDev-Email": "owner@example.com"}
        )
    assert [notification["id"] for notification in response.json()] == [mine.id]


async def test_opening_the_inbox_marks_only_the_person_s_notices_read(tmp_path, druks_db):
    owner = await Account.get_or_create(druks_db, "owner@example.com")
    other = await Account.get_or_create(druks_db, "other@example.com")
    mine = await Notification.create(druks_db, account_id=owner.id, reason="r", body="b")
    theirs = await Notification.create(druks_db, account_id=other.id, reason="r", body="b")
    await druks_db.commit()
    settings = make_settings(tmp_path, identity={"mode": "header", "header": "X-ExeDev-Email"})
    app = configure_app_for_test(settings=settings, authenticated=False)

    with TestClient(app) as client:
        response = client.post(
            "/api/notifications/inbox/read", headers={"X-ExeDev-Email": "owner@example.com"}
        )
    assert response.status_code == 204
    await druks_db.refresh(mine)
    await druks_db.refresh(theirs)
    assert mine.read_at is not None
    assert theirs.read_at is None


async def test_endpoints_list_and_get_omit_the_token(tmp_path, druks_db):
    destination = await _destination()
    tokens = []
    for index in range(3):
        notification = await Notification.create(
            druks_db,
            destination_id=destination.id,
            reason="gate.parked",
            body=f"note {index}",
            subject=_SUBJECT,
        )
        tokens.append(notification.correlation_token)
    failed = await Notification.create(
        druks_db, destination_id=destination.id, reason="gate.parked", body="bad", subject=_SUBJECT
    )
    await failed.mark_failed("DeliveryError: HTTPStatusError")
    tokens.append(failed.correlation_token)

    with TestClient(configure_app_for_test(settings=make_settings(tmp_path))) as client:
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


async def test_destinations_route_still_resolves_after_notifications_mount(tmp_path, druks_db):
    # The route-order pin: the notifications /{notification_id} match must not
    # swallow /api/notifications/destinations.
    await _destination(name="alpha")

    with TestClient(configure_app_for_test(settings=make_settings(tmp_path))) as client:
        listed = client.get("/api/notifications/destinations")
        assert listed.status_code == 200
        assert [destination["name"] for destination in listed.json()] == ["alpha"]
        assert listed.json()[0]["channelId"] == "C1"


async def test_settings_gate_park_destination_set_clear_and_reject(tmp_path, druks_db):
    destination = await _destination()

    with TestClient(configure_app_for_test(settings=make_settings(tmp_path))) as client:
        assert client.get("/api/settings").json()["gateParkDestinationId"] is None

        set_response = client.patch("/api/settings", json={"gateParkDestinationId": destination.id})
        assert set_response.status_code == 200
        assert set_response.json()["gateParkDestinationId"] == destination.id
        assert client.get("/api/settings").json()["gateParkDestinationId"] == destination.id

        unknown = client.patch("/api/settings", json={"gateParkDestinationId": "no-such-id"})
        assert unknown.status_code == 422

        cleared = client.patch("/api/settings", json={"gateParkDestinationId": None})
        assert cleared.status_code == 200
        assert cleared.json()["gateParkDestinationId"] is None


async def test_deleting_designated_destination_unsets_the_pointer(tmp_path, druks_db):
    destination = await _destination()

    with TestClient(configure_app_for_test(settings=make_settings(tmp_path))) as client:
        client.patch("/api/settings", json={"gateParkDestinationId": destination.id})

        deleted = client.delete(f"/api/notifications/destinations/{destination.id}")
        assert deleted.status_code == 204
        assert client.get("/api/settings").json()["gateParkDestinationId"] is None


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
        kind="notifications.test",
        input_gate="review",
        input_request=ask,
        input_requested_at=Base.utc_now(),
    )
    druks_db.add(run)
    await druks_db.flush()
    await seed_dbos_status(druks_db, run.id, run_state)
    destination = await _destination(name=f"dest-{run.id[-8:]}")
    notification = await Notification.create(
        druks_db,
        destination_id=destination.id,
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
        kind="notifications.test",
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
    )

    assert resume_spy == [{"id": run.id, "action": "approve", "answers": {"q1": "a"}, "note": ""}]
    assert (await druks_db.get(Notification, notification.id)).state == "acknowledged"
    assert (await druks_db.get(Notification, notification.id)).is_acknowledged


async def test_only_the_chat_s_person_answers_in_its_thread(druks_db, resume_spy):
    _, notification = await _thread_notification(druks_db, ask={**_IN_APP_ASK, "questions": []})

    with pytest.raises(AnswerNotAllowedError):
        await respond_to_notification(
            druks_db, notification.correlation_token, {"control": "approve"}, user_id="U2"
        )

    assert resume_spy == []
    assert not notification.is_acknowledged


async def test_a_click_that_answers_in_a_thread_tells_the_chat(druks_db, resume_spy):
    run, notification = await _thread_notification(druks_db, ask={**_IN_APP_ASK, "questions": []})

    await respond_to_notification(
        druks_db, notification.correlation_token, {"control": "approve"}, user_id="U1"
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

    await respond_to_notification(
        druks_db, notification.correlation_token, {"control": control}, user_id="U1"
    )

    assert resume_spy == []
    assert notification.is_acknowledged
    messages = await druks_db.execute(
        select(Message.body, Message.is_internal).where(
            Message.conversation_id == run.conversation_id
        )
    )
    assert messages.all() == [(label, False)]
    started.assert_awaited_once_with(deliver, run.conversation_id)


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
    destination = await _destination(name="runless-dest")
    notification = await Notification.create(
        druks_db, destination_id=destination.id, reason="r", body="b", subject=_SUBJECT
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
        kind="notifications.test",
        input_gate="review",
        input_request=_IN_APP_ASK,
        input_requested_at=Base.utc_now(),
    )
    druks_db.add(run)
    await druks_db.flush()
    await seed_dbos_status(druks_db, run.id, "parked")
    destination = await _destination(name="stale-dest")
    notification = await Notification.create(
        druks_db,
        destination_id=destination.id,
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
        )
    with pytest.raises(InvalidChoiceError):
        await respond_to_notification(
            druks_db,
            notification.correlation_token,
            {"control": "request_changes", "note": "   "},
            user_id=None,
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

from datetime import timedelta

import pytest
from conftest import (
    configure_app_for_test,
    make_settings,
    seed_dbos_status,
)
from druks.durable import Run
from druks.models import Base
from druks.notifications.exceptions import InvalidChoiceError
from druks.notifications.models import Destination, Notification
from druks.notifications.services import respond_to_notification
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError
from uuid_utils import uuid7

_WEBHOOK_URL = "https://hooks.slack.com/services/T000/B000/notifsecret"
_SUBJECT = {"type": "work_item", "id": 1}


def _destination(name: str = "ops") -> Destination:
    return Destination.create(name=name, kind="slack_webhook", url=_WEBHOOK_URL)


# --- entity ------------------------------------------------------------------


def test_unique_token_collision_raises(db_session):
    destination = _destination()
    first = Notification.create(
        destination_id=destination.id, reason="r", body="b", subject=_SUBJECT
    )

    duplicate = Notification(
        destination_id=destination.id,
        reason="r",
        body="b",
        subject=_SUBJECT,
        correlation_token=first.correlation_token,
    )
    db_session.add(duplicate)
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_list_recent_newest_first_with_limit(db_session):
    destination = _destination()
    ids = [
        Notification.create(
            destination_id=destination.id, reason="r", body=f"note {i}", subject=_SUBJECT
        ).id
        for i in range(3)
    ]

    listed = Notification.list_recent(limit=2)
    assert [notification.id for notification in listed] == [ids[2], ids[1]]


# --- read endpoints ---------------------------------------------------------


def test_endpoints_list_and_get_omit_the_token(tmp_path, db_session):
    destination = _destination()
    tokens = []
    for index in range(3):
        notification = Notification.create(
            destination_id=destination.id,
            reason="gate.parked",
            body=f"note {index}",
            subject=_SUBJECT,
        )
        tokens.append(notification.correlation_token)
    failed = Notification.create(
        destination_id=destination.id, reason="gate.parked", body="bad", subject=_SUBJECT
    )
    failed.mark_failed("DeliveryError: HTTPStatusError")
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
        assert _WEBHOOK_URL not in listed.text

        one = client.get(f"/api/notifications/{failed.id}")
        assert one.status_code == 200
        assert one.json()["id"] == failed.id
        assert one.json()["attempts"] == 0
        assert failed.correlation_token not in one.text
        assert _WEBHOOK_URL not in one.text

        assert client.get("/api/notifications/no-such-id").status_code == 404


def test_destinations_route_still_resolves_after_notifications_mount(tmp_path, db_session):
    # The route-order pin: the notifications /{notification_id} match must not
    # swallow /api/notifications/destinations.
    _destination(name="alpha")

    with TestClient(configure_app_for_test(settings=make_settings(tmp_path))) as client:
        listed = client.get("/api/notifications/destinations")
        assert listed.status_code == 200
        assert [destination["name"] for destination in listed.json()] == ["alpha"]
        assert listed.json()[0]["hasUrl"] is True


# --- the gate-park destination setting ---------------------------------------


def test_settings_gate_park_destination_set_clear_and_reject(tmp_path, db_session):
    destination = _destination()

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


def test_deleting_designated_destination_unsets_the_pointer(tmp_path, db_session):
    destination = _destination()

    with TestClient(configure_app_for_test(settings=make_settings(tmp_path))) as client:
        client.patch("/api/settings", json={"gateParkDestinationId": destination.id})

        deleted = client.delete(f"/api/notifications/destinations/{destination.id}")
        assert deleted.status_code == 204
        assert client.get("/api/settings").json()["gateParkDestinationId"] is None


# --- respond: the inbound half ------------------------------------------------

_IN_APP_ASK = {
    "presentation": "in_app",
    "controls": ["approve", "request_changes", "cancel"],
    "questions": [{"id": "q1", "prompt": "which?", "options": [{"id": "a", "label": "A"}]}],
}


def _parked_notification(db_session, *, ask=None, run_state="pending_input"):
    ask = ask or _IN_APP_ASK
    run = Run(
        id=str(uuid7()),
        kind="notifications.test",
        input_gate="review",
        input_request=ask,
        input_requested_at=Base.utc_now(),
    )
    db_session.add(run)
    db_session.flush()
    seed_dbos_status(db_session, run.id, run_state)
    destination = _destination(name=f"dest-{run.id[-8:]}")
    notification = Notification.create(
        destination_id=destination.id,
        reason="gate.parked",
        body="review the plan",
        subject={"type": "notification_probe", "id": 1},
        run_id=run.id,
        run_parked_at=run.input_requested_at,
    )
    return run, notification


@pytest.fixture
def resume_spy(monkeypatch):
    calls = []

    async def _spy(self, **fields):
        calls.append({"id": self.id, **fields})

    monkeypatch.setattr(Run, "resume", _spy)
    return calls


async def test_respond_resumes_and_marks_acknowledged(db_session, resume_spy):
    run, notification = _parked_notification(db_session)

    await respond_to_notification(
        notification.correlation_token,
        {"control": "approve", "answers": {"q1": "a"}, "note": ""},
    )

    assert resume_spy == [{"id": run.id, "action": "approve", "answers": {"q1": "a"}, "note": ""}]
    assert Notification.get(notification.id).state == "acknowledged"
    assert Notification.get(notification.id).is_acknowledged


async def test_respond_route_codes_and_secret_hygiene(tmp_path, db_session, resume_spy):
    run, notification = _parked_notification(db_session)
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
        assert _WEBHOOK_URL not in response.text


async def test_respond_external_notification_not_answerable(tmp_path, db_session, resume_spy):
    run, notification = _parked_notification(
        db_session, ask={"presentation": "external", "label": "Answer on the ticket"}
    )
    client = TestClient(configure_app_for_test(settings=make_settings(tmp_path)))

    response = client.post(
        f"/_external/notifications/{notification.correlation_token}/respond",
        json={"control": "approve"},
    )

    assert response.status_code == 422
    assert "informational" in response.json()["detail"]
    assert resume_spy == []


async def test_respond_runless_notification_not_answerable(tmp_path, db_session, resume_spy):
    destination = _destination(name="runless-dest")
    notification = Notification.create(
        destination_id=destination.id, reason="r", body="b", subject=_SUBJECT
    )
    client = TestClient(configure_app_for_test(settings=make_settings(tmp_path)))

    response = client.post(
        f"/_external/notifications/{notification.correlation_token}/respond",
        json={"control": "approve"},
    )

    assert response.status_code == 422
    assert resume_spy == []


async def test_respond_stale_round_409(tmp_path, db_session, resume_spy):
    run = Run(
        id=str(uuid7()),
        kind="notifications.test",
        input_gate="review",
        input_request=_IN_APP_ASK,
        input_requested_at=Base.utc_now(),
    )
    db_session.add(run)
    db_session.flush()
    seed_dbos_status(db_session, run.id, "pending_input")
    destination = _destination(name="stale-dest")
    notification = Notification.create(
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


async def test_respond_run_no_longer_parked_409(tmp_path, db_session, resume_spy):
    run, notification = _parked_notification(db_session, run_state="finished")
    client = TestClient(configure_app_for_test(settings=make_settings(tmp_path)))

    response = client.post(
        f"/_external/notifications/{notification.correlation_token}/respond",
        json={"control": "approve"},
    )

    assert response.status_code == 409
    assert resume_spy == []


async def test_respond_corrupt_correlation_500_and_logged(
    tmp_path, db_session, resume_spy, monkeypatch, caplog
):
    run, notification = _parked_notification(db_session)
    monkeypatch.setattr(Run, "get", classmethod(lambda cls, run_id: None))
    client = TestClient(
        configure_app_for_test(settings=make_settings(tmp_path)), raise_server_exceptions=False
    )

    response = client.post(
        f"/_external/notifications/{notification.correlation_token}/respond",
        json={"control": "approve"},
    )

    assert response.status_code == 500
    assert response.json() == {"error": "INTERNAL_ERROR", "detail": "Internal server error"}
    assert "references run" in caplog.text
    assert notification.correlation_token not in response.text
    assert resume_spy == []


async def test_respond_direct_call_rejects_whitespace_only_content(db_session, resume_spy):
    # The core is also the direct-call boundary (the Slack rail bypasses the
    # HTTP models' whitespace stripping) — blank means blank on every path.
    run, notification = _parked_notification(db_session)

    with pytest.raises(InvalidChoiceError):
        await respond_to_notification(
            notification.correlation_token,
            {"control": "approve", "answers": {"q1": "   "}},
        )
    with pytest.raises(InvalidChoiceError):
        await respond_to_notification(
            notification.correlation_token,
            {"control": "request_changes", "note": "   "},
        )
    assert resume_spy == []
    assert Notification.get(notification.id).state == "pending"


async def test_respond_ask_without_presentation_not_answerable(tmp_path, db_session, resume_spy):
    # An ask that doesn't declare in_app isn't answerable via this rail — the
    # mapped 422, not a crash.
    run, notification = _parked_notification(
        db_session, ask={"label": "legacy ask", "controls": ["approve"]}
    )
    client = TestClient(configure_app_for_test(settings=make_settings(tmp_path)))

    response = client.post(
        f"/_external/notifications/{notification.correlation_token}/respond",
        json={"control": "approve"},
    )

    assert response.status_code == 422
    assert resume_spy == []

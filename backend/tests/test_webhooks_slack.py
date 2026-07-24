import hashlib
import hmac
import json
import time
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest
from conftest import configure_app_for_test, make_settings, seed_dbos_status
from druks.core.webhooks.slack import SlackInteractivity, verify_slack_signature
from druks.database import db_session as ambient_db_session
from druks.durable import Run
from druks.models import Base
from druks.notifications.buttons import encode_button
from druks.notifications.models import Destination, Notification
from druks.webhooks.router import match_webhook
from fastapi.testclient import TestClient
from uuid_utils import uuid7

_SIGNING_SECRET = "slack-test-signing-secret"
_WEBHOOK_URL = "https://hooks.slack.com/services/T000/B000/slackrail"

_IN_APP_ASK = {
    "presentation": "in_app",
    "controls": ["approve", "request_changes", "cancel"],
    "questions": [],
}


def _parked_notification(db_session):
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
    destination = Destination.create(
        name=f"slack-{run.id[-8:]}", kind="slack_webhook", url=_WEBHOOK_URL
    )
    notification = Notification.create(
        destination_id=destination.id,
        reason="gate.parked",
        body="review the plan",
        subject={"type": "notification_probe", "id": 1},
        run_id=run.id,
        run_parked_at=run.input_requested_at,
    )
    return run, notification


def _signed_headers(body: bytes, *, timestamp=None, secret=_SIGNING_SECRET):
    timestamp = timestamp or str(int(time.time()))
    digest = hmac.new(secret.encode(), b"v0:" + timestamp.encode() + b":" + body, hashlib.sha256)
    return {
        "x-slack-request-timestamp": timestamp,
        "x-slack-signature": "v0=" + digest.hexdigest(),
        "content-type": "application/x-www-form-urlencoded",
    }


def _interactivity_body(action_id: str) -> bytes:
    payload = {"type": "block_actions", "actions": [{"action_id": action_id}]}
    return urlencode({"payload": json.dumps(payload)}).encode()


def _client(tmp_path) -> TestClient:
    return TestClient(
        configure_app_for_test(
            settings=make_settings(tmp_path, slack_signing_secret=_SIGNING_SECRET)
        )
    )


@pytest.fixture
def resume_spy(monkeypatch):
    calls = []

    async def _spy(self, **fields):
        calls.append({"id": self.id, **fields})

    monkeypatch.setattr(Run, "resume", _spy)
    return calls


def test_match_webhook_resolves_slack_interactivity():
    cls, kwargs = match_webhook("slack/interactivity/")
    assert cls is SlackInteractivity
    assert kwargs == {}


def test_signature_verifier_against_a_known_answer(monkeypatch):
    # A fixed vector computed independently of the code under test (and of the
    # _signed_headers helper), so a mirror-image bug in the base-string
    # construction can't hide: the verifier is held to Slack's
    # v0:{ts}:{body} algorithm itself. Clock frozen to the vector's epoch.
    body = b"payload=%7B%22type%22%3A%22ping%22%7D"
    timestamp = "1234567890"
    known = "v0=b14d6fbbfab2d9b2cb99c67b41b6289778e8bf6f4b534b69451b9e7ba1f878dd"
    monkeypatch.setattr(
        "druks.core.webhooks.slack.time", SimpleNamespace(time=lambda: 1234567890.0)
    )

    verify_slack_signature(body, known, timestamp, "slack-test-signing-secret")

    bit_flipped = known[:-1] + ("0" if known[-1] != "0" else "1")
    with pytest.raises(Exception) as rejected:
        verify_slack_signature(body, bit_flipped, timestamp, "slack-test-signing-secret")
    assert getattr(rejected.value, "status_code", None) == 401

    with pytest.raises(Exception) as tampered_body:
        verify_slack_signature(b"tampered" + body, known, timestamp, "slack-test-signing-secret")
    assert getattr(tampered_body.value, "status_code", None) == 401


async def test_unsigned_or_stale_requests_401_and_never_resume(tmp_path, db_session, resume_spy):
    run, notification = _parked_notification(db_session)
    body = _interactivity_body(encode_button(notification.correlation_token, "approve"))
    with _client(tmp_path) as client:
        no_headers = client.post("/_external/slack/interactivity/", content=body)
        assert no_headers.status_code == 401

        wrong_secret = client.post(
            "/_external/slack/interactivity/",
            content=body,
            headers=_signed_headers(body, secret="wrong-secret"),
        )
        assert wrong_secret.status_code == 401

        replayed = client.post(
            "/_external/slack/interactivity/",
            content=body,
            headers=_signed_headers(body, timestamp=str(int(time.time()) - 3600)),
        )
        assert replayed.status_code == 401

    assert resume_spy == []
    assert Notification.get(notification.id).state == "pending"
    for response in (no_headers, wrong_secret, replayed):
        assert _SIGNING_SECRET not in response.text
        assert notification.correlation_token not in response.text


async def test_signed_click_routes_through_respond(tmp_path, db_session, resume_spy):
    run, notification = _parked_notification(db_session)
    body = _interactivity_body(encode_button(notification.correlation_token, "approve"))
    with _client(tmp_path) as client:
        response = client.post(
            "/_external/slack/interactivity/", content=body, headers=_signed_headers(body)
        )

    assert response.status_code == 200
    assert response.json() == {"accepted": True}
    assert resume_spy == [{"id": run.id, "action": "approve", "answers": {}, "note": ""}]
    # The request's own session committed the transition; drop the ambient
    # (task-scoped) session's cached instance to read it.
    ambient_db_session().expire_all()
    assert Notification.get(notification.id).state == "acknowledged"
    assert notification.correlation_token not in response.text
    assert _SIGNING_SECRET not in response.text


async def test_dead_round_click_is_acknowledged_without_resume(tmp_path, db_session, resume_spy):
    run, notification = _parked_notification(db_session)
    notification.mark_acknowledged()
    with _client(tmp_path) as client:
        body = _interactivity_body(encode_button(notification.correlation_token, "approve"))
        acknowledged = client.post(
            "/_external/slack/interactivity/", content=body, headers=_signed_headers(body)
        )
        assert acknowledged.status_code == 200
        assert acknowledged.json() == {"accepted": False}

        ghost = _interactivity_body(encode_button("ghost-token", "approve"))
        unknown = client.post(
            "/_external/slack/interactivity/", content=ghost, headers=_signed_headers(ghost)
        )
        assert unknown.status_code == 200
    assert unknown.json() == {"accepted": False}
    assert "ghost-token" not in unknown.text

    assert resume_spy == []


async def test_malformed_payloads_400_never_500_never_resume(tmp_path, db_session, resume_spy):
    run, notification = _parked_notification(db_session)
    malformed = [
        b"not-a-form",
        urlencode({"payload": "not json"}).encode(),
        urlencode({"payload": json.dumps({"actions": [{"action_id": "x.y"}]})}).encode(),
        urlencode({"payload": json.dumps({"type": "block_actions"})}).encode(),
        urlencode({"payload": json.dumps({"type": "block_actions", "actions": []})}).encode(),
        _interactivity_body("not-an-encoded-button!!"),
    ]

    with _client(tmp_path) as client:
        for body in malformed:
            response = client.post(
                "/_external/slack/interactivity/", content=body, headers=_signed_headers(body)
            )
            assert response.status_code == 400

    assert resume_spy == []
    assert Notification.get(notification.id).state == "pending"


async def test_unknown_interactivity_type_is_acknowledged_unhandled(
    tmp_path, db_session, resume_spy
):
    _parked_notification(db_session)
    body = urlencode({"payload": json.dumps({"type": "view_submission"})}).encode()

    with _client(tmp_path) as client:
        response = client.post(
            "/_external/slack/interactivity/", content=body, headers=_signed_headers(body)
        )

    assert response.status_code == 200
    assert response.json() == {"accepted": True, "handled": False}
    assert resume_spy == []

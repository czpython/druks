import hashlib
import hmac
import json
import time
from datetime import timedelta
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest
from conftest import bind_ambient_session, install_service
from druks.accounts.enums import AccountKind
from druks.accounts.models import Account
from druks.chat.channels.whatsapp.constants import WAHA_AUDIENCE
from druks.chat.enums import ConversationSource
from druks.chat.models import Conversation
from druks.core.webhooks.slack import SlackInteractivity, verify_slack_signature
from druks.db import db_session as ambient_db_session
from druks.durable import Run
from druks.models import Base
from druks.notifications.buttons import encode_button
from druks.notifications.models import Destination, Notification
from druks.secrets.enums import SecretKind
from druks.secrets.models import VaultSecret
from druks.testing import configure_app_for_test, make_settings, seed_dbos_status
from druks.webhooks.router import match_webhook
from fastapi.testclient import TestClient
from slack_sdk.webhook.async_client import AsyncWebhookClient
from uuid_utils import uuid7

_SIGNING_SECRET = "slack-test-signing-secret"
_RESPONSE_URL = "https://hooks.slack.com/actions/T1/1/click"
_QUESTION = {"type": "section", "text": {"type": "mrkdwn", "text": "review the plan"}}

_IN_APP_ASK = {
    "presentation": "in_app",
    "controls": ["approve", "request_changes"],
    "questions": [],
}


async def _parked_notification(druks_db):
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
    destination = await Destination.create(
        druks_db, name=f"slack-{run.id[-8:]}", kind="slack_channel", channel_id="C1"
    )
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


def _signed_headers(body: bytes, *, timestamp=None, secret=_SIGNING_SECRET):
    timestamp = timestamp or str(int(time.time()))
    digest = hmac.new(secret.encode(), b"v0:" + timestamp.encode() + b":" + body, hashlib.sha256)
    return {
        "x-slack-request-timestamp": timestamp,
        "x-slack-signature": "v0=" + digest.hexdigest(),
        "content-type": "application/x-www-form-urlencoded",
    }


def _interactivity_payload(action_id: str) -> dict:
    button = {"type": "button", "text": {"type": "plain_text", "text": "Approve"}}
    return {
        "type": "block_actions",
        "team": {"id": "T1"},
        "actions": [{**button, "action_id": action_id}],
        "message": {
            "text": "review the plan",
            "blocks": [_QUESTION, {"type": "actions", "elements": [button]}],
        },
        "response_url": _RESPONSE_URL,
    }


def _interactivity_body(action_id: str) -> bytes:
    return urlencode({"payload": json.dumps(_interactivity_payload(action_id))}).encode()


def _client(tmp_path) -> TestClient:
    return TestClient(configure_app_for_test(settings=make_settings(tmp_path)))


@pytest.fixture
async def slack_card(druks_db):
    """The Slack card whose signing secret signs every delivery."""
    bind_ambient_session(druks_db)
    await install_service(
        "slack",
        access_token="xoxb",
        identity={"client_id": "c1", "team": "Acme", "team_id": "T1", "bot_user_id": "U900"},
        secrets={"client_secret": "cs", "signing_secret": _SIGNING_SECRET},
    )
    await druks_db.commit()


@pytest.fixture
def slack_replies(monkeypatch):
    replies = []

    async def _send(self, **reply):
        replies.append({"url": self.url, **reply})

    monkeypatch.setattr(AsyncWebhookClient, "send", _send)
    return replies


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


async def test_unsigned_or_stale_requests_401_and_never_resume(
    tmp_path, druks_db, resume_spy, slack_card
):
    run, notification = await _parked_notification(druks_db)
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
    assert (await ambient_db_session().get(Notification, notification.id)).state == "pending"
    for response in (no_headers, wrong_secret, replayed):
        assert _SIGNING_SECRET not in response.text
        assert notification.correlation_token not in response.text


async def test_signed_click_routes_through_respond(
    tmp_path, druks_db, resume_spy, slack_card, slack_replies
):
    run, notification = await _parked_notification(druks_db)
    body = _interactivity_body(encode_button(notification.correlation_token, "approve"))
    with _client(tmp_path) as client:
        response = client.post(
            "/_external/slack/interactivity/", content=body, headers=_signed_headers(body)
        )

    assert response.status_code == 200
    assert response.json() == {"accepted": True}
    assert resume_spy == [{"id": run.id, "action": "approve", "answers": {}, "note": ""}]
    assert slack_replies == [
        {
            "url": _RESPONSE_URL,
            "text": "review the plan",
            "blocks": [
                _QUESTION,
                {"type": "section", "text": {"type": "mrkdwn", "text": "Answer: Approve"}},
            ],
            "replace_original": True,
        }
    ]
    # The request's own session committed the transition; drop the ambient
    # (task-scoped) session's cached instance to read it.
    ambient_db_session().expunge_all()
    assert (await ambient_db_session().get(Notification, notification.id)).state == "acknowledged"
    assert notification.correlation_token not in response.text
    assert _SIGNING_SECRET not in response.text


async def test_answer_line_escapes_mrkdwn_in_the_button_label(
    tmp_path, druks_db, resume_spy, slack_card, slack_replies
):
    run, notification = await _parked_notification(druks_db)
    payload = _interactivity_payload(encode_button(notification.correlation_token, "approve"))
    payload["actions"][0]["text"]["text"] = "Tom & Jerry <A>"
    body = urlencode({"payload": json.dumps(payload)}).encode()
    with _client(tmp_path) as client:
        response = client.post(
            "/_external/slack/interactivity/", content=body, headers=_signed_headers(body)
        )

    assert response.json() == {"accepted": True}
    answer = slack_replies[0]["blocks"][-1]
    assert answer == {
        "type": "section",
        "text": {"type": "mrkdwn", "text": "Answer: Tom &amp; Jerry &lt;A&gt;"},
    }


async def test_click_with_a_control_outside_the_ask_tells_the_clicker_plainly(
    tmp_path, druks_db, resume_spy, slack_card, slack_replies
):
    run, notification = await _parked_notification(druks_db)
    body = _interactivity_body(encode_button(notification.correlation_token, "delete"))
    with _client(tmp_path) as client:
        response = client.post(
            "/_external/slack/interactivity/", content=body, headers=_signed_headers(body)
        )

    assert response.json() == {"accepted": False}
    assert resume_spy == []
    assert slack_replies == [
        {
            "url": _RESPONSE_URL,
            "text": "This button no longer matches the question.",
            "response_type": "ephemeral",
            "replace_original": False,
        }
    ]
    # The raw control id never reaches the clicker.
    assert "delete" not in slack_replies[0]["text"]


async def test_dead_round_click_is_acknowledged_without_resume(
    tmp_path, druks_db, resume_spy, slack_card, slack_replies
):
    run, notification = await _parked_notification(druks_db)
    await notification.mark_acknowledged()
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
    assert slack_replies == [
        {
            "url": _RESPONSE_URL,
            "text": text,
            "response_type": "ephemeral",
            "replace_original": False,
        }
        for text in ("This notification was already acknowledged.", "Unknown respond token.")
    ]
    assert "ghost-token" not in unknown.text

    assert resume_spy == []


async def test_click_after_the_run_parked_again_tells_the_clicker(
    tmp_path, druks_db, resume_spy, slack_card, slack_replies
):
    run, notification = await _parked_notification(druks_db)
    notification.run_parked_at = run.input_requested_at - timedelta(minutes=1)
    await druks_db.flush()
    body = _interactivity_body(encode_button(notification.correlation_token, "approve"))
    with _client(tmp_path) as client:
        response = client.post(
            "/_external/slack/interactivity/", content=body, headers=_signed_headers(body)
        )

    assert response.json() == {"accepted": False}
    assert resume_spy == []
    assert slack_replies == [
        {
            "url": _RESPONSE_URL,
            "text": "The run has moved past the round this notification was sent for.",
            "response_type": "ephemeral",
            "replace_original": False,
        }
    ]


async def test_click_on_a_number_admins_question_tells_the_clicker(
    tmp_path, druks_db, resume_spy, slack_card, slack_replies
):
    run, notification = await _parked_notification(druks_db)
    admin = await Account.create_for_bot(druks_db, AccountKind.BOT_ADMIN)
    connection = await VaultSecret.store(
        druks_db,
        SecretKind.SESSION,
        WAHA_AUDIENCE,
        secrets={},
        identity={"app": "helpdesk", "admin": {"account_id": admin.id}},
        account_id=run.account_id,
    )
    conversation = await Conversation.get_or_create_for_user(
        druks_db,
        connection,
        run.account_id,
        source=ConversationSource.WHATSAPP,
        user_id="41000000001@c.us",
        user_name="",
        user_phone="",
        thread_id="",
    )
    run.conversation_id = conversation.id
    await druks_db.commit()
    body = _interactivity_body(encode_button(notification.correlation_token, "approve"))
    with _client(tmp_path) as client:
        response = client.post(
            "/_external/slack/interactivity/", content=body, headers=_signed_headers(body)
        )

    assert response.status_code == 200
    assert response.json() == {"accepted": False}
    assert resume_spy == []
    assert slack_replies == [
        {
            "url": _RESPONSE_URL,
            "text": "Only the admin of the number that started this run can answer it.",
            "response_type": "ephemeral",
            "replace_original": False,
        }
    ]


async def test_malformed_payloads_400_never_500_never_resume(
    tmp_path, druks_db, resume_spy, slack_card
):
    run, notification = await _parked_notification(druks_db)
    malformed = [
        b"not-a-form",
        urlencode({"payload": "not json"}).encode(),
        urlencode({"payload": json.dumps({"actions": [{"action_id": "x.y"}]})}).encode(),
        urlencode({"payload": json.dumps({"type": "block_actions"})}).encode(),
        urlencode({"payload": json.dumps({"type": "block_actions", "actions": []})}).encode(),
        _interactivity_body("not-an-encoded-button!!"),
    ]
    # A real button for the parked round, each time without one field the reply needs.
    for drop in (
        lambda payload: payload.pop("response_url"),
        lambda payload: payload.pop("message"),
        lambda payload: payload["message"].pop("text"),
        lambda payload: payload["actions"][0].pop("text"),
    ):
        payload = _interactivity_payload(encode_button(notification.correlation_token, "approve"))
        drop(payload)
        malformed.append(urlencode({"payload": json.dumps(payload)}).encode())

    with _client(tmp_path) as client:
        for body in malformed:
            response = client.post(
                "/_external/slack/interactivity/", content=body, headers=_signed_headers(body)
            )
            assert response.status_code == 400

    assert resume_spy == []
    assert (await ambient_db_session().get(Notification, notification.id)).state == "pending"


async def test_unknown_interactivity_type_is_acknowledged_unhandled(
    tmp_path, druks_db, resume_spy, slack_card
):
    await _parked_notification(druks_db)
    body = urlencode({"payload": json.dumps({"type": "view_submission"})}).encode()

    with _client(tmp_path) as client:
        response = client.post(
            "/_external/slack/interactivity/", content=body, headers=_signed_headers(body)
        )

    assert response.status_code == 200
    assert response.json() == {"accepted": True, "handled": False}
    assert resume_spy == []

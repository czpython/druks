import hashlib
import json
import time
from unittest.mock import AsyncMock
from urllib.parse import parse_qsl, urlencode, urlparse

import druks.chat.channels.slack.subscribers  # noqa: F401 — the webhook publishes to it
import httpx
import jwt as pyjwt
import pytest
from conftest import PORTAL_KEY, bind_ambient_session, connect_service
from druks.accounts.models import Account
from druks.chat.models import Conversation
from druks.core.apis.slack import SLACK_BOT_SCOPES, SlackClient
from druks.core.services import Slack
from druks.db import db_session
from druks.durable import Run
from druks.models import Base
from druks.notifications.buttons import encode_button
from druks.notifications.models import Destination, Notification
from druks.secrets.datastructures import Audience
from druks.secrets.models import VaultSecret
from druks.services import Service
from druks.testing import asgi_client, configure_app_for_test, make_settings, seed_dbos_status
from uuid_utils import uuid7

INSTANCE_ID = "6f2c1c1e-9b8e-4b65-9d3c-7a2b1f4e8d10"
AUDIENCE = f"druks:{INSTANCE_ID}"
PORTAL = "https://portal.test"
CARD_URL = "https://manager.test/slack"
MANAGER_TOKEN = "manager-secret"
CLIENT_ID = "1234.5678"
BOT_TOKEN = "xoxb-cloud"
ANA = "U100"
BOT = "U900"
CONFIGURATION = f"""[manager]
name = "Manager"
jwks_url = "{PORTAL}/.well-known/jwks.json"
issuer = "{PORTAL}"
audience = "{AUDIENCE}"
services = ["slack"]

[urls]
endpoint = "https://instance.test"

[services.slack]
url = "{CARD_URL}"
client_id = "{CLIENT_ID}"
"""
# The card after the install: the table's fields and what the bot token proves.
FACTS_AFTER_INSTALL = {
    "url": CARD_URL,
    "client_id": CLIENT_ID,
    "team": "Acme",
    "team_id": "T1",
    "bot_name": "druks",
    "bot_user_id": BOT,
}
# What auth.test answers for each token.
IDENTITIES = {
    BOT_TOKEN: {"team": "Acme", "team_id": "T1", "user": "druks", "user_id": BOT},
    "xoxp-ana": {"team": "Acme", "team_id": "T1", "user": "ana", "user_id": ANA},
    "xoxp-other": {"team": "Other", "team_id": "T2", "user": "ana", "user_id": ANA},
}
EVENTS = "/_external/slack/events/"
INTERACTIVITY = "/_external/slack/interactivity/"


class _Manager:
    """The manager's token exchange, answering at the transport: what it was asked, and the
    ``answer`` it gives. It answers an install by default."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.exchanges: list[dict] = []
        self.answer = {
            "ok": True,
            "access_token": BOT_TOKEN,
            "token_type": "bot",
            "bot_user_id": BOT,
            "team": {"id": "T1", "name": "Acme"},
            "authed_user": {"id": ANA},
        }

    async def handle(self, request: httpx.Request) -> httpx.Response:
        assert str(request.url) == f"{CARD_URL}/oauth/token"
        self.requests.append(request)
        self.exchanges.append(dict(parse_qsl(request.content.decode())))
        return httpx.Response(200, request=request, json=self.answer)


@pytest.fixture
async def managed_slack(tmp_path, monkeypatch, druks_db):
    """The settings of a Druks whose Slack app the manager owns, its card synced."""
    (tmp_path / "druks.toml").write_text(CONFIGURATION)
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "manager.token").write_text(MANAGER_TOKEN)
    monkeypatch.setenv("DRUKS_CONFIG", str(tmp_path / "druks.toml"))
    monkeypatch.setenv("DRUKS_SECRETS_DIR", str(secrets))
    bind_ambient_session(druks_db)
    await Service.sync_configuration(druks_db)
    return make_settings(tmp_path)


@pytest.fixture
async def card_after_install(managed_slack):
    await connect_service("slack", identity=FACTS_AFTER_INSTALL, secrets={"bot_token": BOT_TOKEN})


@pytest.fixture
def manager(monkeypatch):
    fake = _Manager()

    async def handle_async_request(transport, request):
        return await fake.handle(request)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", handle_async_request)
    return fake


@pytest.fixture
def slack(monkeypatch):
    """Slack's Web API, answered locally. Records each call's token, method, and arguments."""
    calls = []

    async def api_call(client, api_method, *, params=None, json=None, **request):
        calls.append((client.token, api_method, json or params or {}))
        if api_method == "auth.test":
            return {"ok": True, **IDENTITIES[client.token]}
        return {"ok": True, "channel": "D1", "ts": f"{len(calls)}.0"}

    monkeypatch.setattr(SlackClient, "api_call", api_call)
    return calls


@pytest.fixture
def delivery(monkeypatch):
    started = AsyncMock()
    monkeypatch.setattr("druks.chat.channels.slack.channel.DBOS.start_workflow_async", started)
    return started


@pytest.fixture
def resume_spy(monkeypatch):
    calls = []

    async def resume(self, **fields):
        calls.append({"id": self.id, **fields})

    monkeypatch.setattr(Run, "resume", resume)
    return calls


def _forwarded(body: bytes, delivery_id: str, content_type: str) -> dict[str, str]:
    """The headers of a delivery that the manager forwards, signed with its key."""
    issued_at = int(time.time())
    signature = pyjwt.encode(
        {
            "iss": PORTAL,
            "aud": AUDIENCE,
            "sub": f"slack:{delivery_id}",
            "body_sha256": hashlib.sha256(body).hexdigest(),
            "iat": issued_at,
            "exp": issued_at + 300,
        },
        PORTAL_KEY,
        algorithm="RS256",
        headers={"kid": "portal-1"},
    )
    return {"Content-Type": content_type, "X-Druks-Signature": signature}


def _message(event_id: str, team_id: str = "T1", user: str = ANA) -> bytes:
    """A person, Ana by default, tags the bot in a room, with a file."""
    event = {
        "type": "message",
        "subtype": "file_share",
        "channel": "C1",
        "channel_type": "channel",
        "user": user,
        "text": f"<@{BOT}> read this",
        "ts": "10.0",
        "files": [
            {
                "id": "F1",
                "name": "plan.pdf",
                "mimetype": "application/pdf",
                "url_private_download": "https://files.slack.com/F1/plan.pdf",
            }
        ],
    }
    body = {"type": "event_callback", "team_id": team_id, "event_id": event_id, "event": event}
    return json.dumps(body).encode()


def _click(button: str, action_ts: str, team_id: str = "T1") -> bytes:
    payload = {
        "type": "block_actions",
        "team": {"id": team_id},
        "actions": [{"action_id": button, "action_ts": action_ts}],
    }
    return urlencode({"payload": json.dumps(payload)}).encode()


async def _get_card(client: httpx.AsyncClient) -> dict:
    """The Slack card, as the Services page reads it."""
    entries = (await client.get("/api/services")).json()
    return next(entry for entry in entries if entry["slug"] == "slack")


async def _install(client: httpx.AsyncClient) -> tuple[dict, httpx.Response]:
    """Start the install at the card's door and come back through the callback. Returns
    the consent query and the callback page."""
    door = (await _get_card(client))["installUrl"]
    consent = urlparse((await client.get(door)).headers["location"])
    query = dict(parse_qsl(consent.query))
    assert consent.geturl().startswith("https://slack.com/oauth/v2/authorize?")
    page = await client.get("/api/oauth/callback", params={"state": query["state"], "code": "c-1"})
    return query, page


async def test_the_card_waits_for_its_install_and_keeps_it_across_a_restart(
    managed_slack, manager, slack
):
    card = await Slack.get()
    assert (card.identity, card.secrets) == ({"url": CARD_URL, "client_id": CLIENT_ID}, {})
    assert not await Slack.is_connected()

    async with asgi_client(configure_app_for_test(settings=managed_slack)) as client:
        entry = await _get_card(client)
        assert not entry["connected"] and entry["managed"]
        assert entry["installUrl"] == "/api/oauth/slack/connect?install=1"

        query, page = await _install(client)
        assert page.status_code == 200
        assert query["scope"] == ",".join(SLACK_BOT_SCOPES)
        assert not {"user_scope", "code_challenge", "code_challenge_method"} & query.keys()
        assert query["state"].startswith(f"{AUDIENCE}.")
        assert query["redirect_uri"] == f"{PORTAL}/slack/oauth/callback"
        assert query["client_id"] == CLIENT_ID

        entry = await _get_card(client)
        assert entry["connected"]
        assert entry["facts"] == FACTS_AFTER_INSTALL

    assert manager.exchanges == [
        {
            "grant_type": "authorization_code",
            "code": "c-1",
            "redirect_uri": f"{PORTAL}/slack/oauth/callback",
            "scope": ",".join(SLACK_BOT_SCOPES),
            "client_id": CLIENT_ID,
        }
    ]
    [token_request] = manager.requests
    assert token_request.headers["Authorization"] == f"Bearer {MANAGER_TOKEN}"
    db_session().expunge_all()
    assert not await VaultSecret.list_connections(db_session(), Audience.service("slack"))

    await Service.sync_configuration(db_session())

    card = await Slack.get()
    assert (card.identity, card.secrets) == (FACTS_AFTER_INSTALL, {"bot_token": BOT_TOKEN})
    assert await Slack.is_connected()
    assert slack == [(BOT_TOKEN, "auth.test", {})]


async def test_a_refused_install_stores_nothing(managed_slack, manager, slack):
    manager.answer = {"ok": False, "error": "invalid_code"}

    async with asgi_client(configure_app_for_test(settings=managed_slack)) as client:
        _, page = await _install(client)

    assert page.status_code == 400
    db_session().expunge_all()
    card = await Slack.get()
    assert (card.identity, card.secrets) == ({"url": CARD_URL, "client_id": CLIENT_ID}, {})
    assert not slack


async def test_a_personal_link_keeps_the_persons_token_apart_and_in_the_cards_workspace(
    card_after_install, managed_slack, manager, slack
):
    async with asgi_client(configure_app_for_test(settings=managed_slack)) as client:
        grants = [
            {
                "ok": True,
                "authed_user": {"id": ANA, "scope": "users:read", "access_token": access_token},
                "team": {"id": "T1"},
            }
            for access_token in ("xoxp-other", "xoxp-ana")
        ]
        pages = []
        for answer in ({"ok": False, "error": "invalid_code"}, *grants):
            manager.answer = answer
            consent = urlparse((await client.get("/api/oauth/slack/connect")).headers["location"])
            query = dict(parse_qsl(consent.query))
            assert query["user_scope"] == "users:read"
            assert not {"scope", "code_challenge"} & query.keys()
            pages.append(
                await client.get(
                    "/api/oauth/callback", params={"state": query["state"], "code": "c-2"}
                )
            )

    assert [page.status_code for page in pages] == [400, 400, 200]
    assert not {"scope", "code_verifier"} & manager.exchanges[2].keys()
    db_session().expunge_all()
    [connection] = await VaultSecret.list_connections(db_session(), Audience.service("slack"))
    assert connection.account.username == "op@example.com"
    assert connection.secrets["access_token"] == "xoxp-ana"
    assert connection.identity["authority"] == "https://slack.com/T1"
    assert (await Slack.get()).secrets == {"bot_token": BOT_TOKEN}


async def test_forwarded_messages_reach_the_bot_once_and_only_from_its_workspace(
    managed_slack, portal_keys, slack, delivery, druks_db, tmp_path, monkeypatch
):
    monkeypatch.setenv("DRUKS_DATA_DIR", str(tmp_path))
    downloads = []

    async def download(client, url):
        downloads.append((client.token, url))
        return b"%PDF"

    monkeypatch.setattr(SlackClient, "download", download)
    ana = await Account.get_or_create(druks_db, "ana@example.com")
    await VaultSecret.connect(
        druks_db,
        Audience.service("slack"),
        account_id=ana.id,
        refresh_token="",
        scopes=["users:read"],
        identity={"authority": "https://slack.com/T1", "subject": ANA},
    )
    json_type = "application/json"

    async with asgi_client(configure_app_for_test(settings=managed_slack)) as client:
        early = _message("Ev0")
        before_install = await client.post(
            EVENTS, content=early, headers=_forwarded(early, "Ev0", json_type)
        )
        await connect_service(
            "slack", identity=FACTS_AFTER_INSTALL, secrets={"bot_token": BOT_TOKEN}
        )
        responses = []
        for event_id, team_id in (("Ev1", "T1"), ("Ev1", "T1"), ("Ev2", "T2")):
            body = _message(event_id, team_id)
            responses.append(
                await client.post(
                    EVENTS, content=body, headers=_forwarded(body, event_id, json_type)
                )
            )
        stranger = _message("Ev3", user="U200")
        await client.post(EVENTS, content=stranger, headers=_forwarded(stranger, "Ev3", json_type))

    assert before_install.json() == {"accepted": False}
    assert [response.json() for response in responses] == [
        {"accepted": True},
        {"accepted": False, "duplicate": True},
        {"accepted": False},
    ]
    [conversation] = await Conversation.list_for_account(druks_db, ana)
    await druks_db.refresh(conversation, ["messages"])
    assert [(message.source_id, message.file.name) for message in conversation.messages] == [
        ("C1:10.0", "plan.pdf")
    ]
    assert downloads == [(BOT_TOKEN, "https://files.slack.com/F1/plan.pdf")]
    delivery.assert_awaited_once()
    assert [(token, method, sent["channel"]) for token, method, sent in slack] == [
        (BOT_TOKEN, "chat.postMessage", "U200"),
        (BOT_TOKEN, "chat.postMessage", "C1"),
    ]


async def test_a_forwarded_click_answers_its_notification_once(
    card_after_install, managed_slack, portal_keys, druks_db, resume_spy
):
    run = Run(
        account_id=(await Account.get_or_create(druks_db, "op@example.com")).id,
        id=str(uuid7()),
        kind="notifications.test",
        input_gate="review",
        input_request={"presentation": "in_app", "controls": ["approve"], "questions": []},
        input_requested_at=Base.utc_now(),
    )
    druks_db.add(run)
    await druks_db.flush()
    await seed_dbos_status(druks_db, run.id, "parked")
    destination = await Destination.create(
        druks_db, name="slack", kind="slack_webhook", url="https://hooks.slack.com/services/T1"
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
    button = encode_button(notification.correlation_token, "approve")
    form_type = "application/x-www-form-urlencoded"

    async with asgi_client(configure_app_for_test(settings=managed_slack)) as client:
        responses = []
        for body in (
            _click(button, "1.0", team_id="T2"),
            _click(button, "1.1"),
            _click(button, "1.1"),
            _click(button, "1.2"),
        ):
            key = "interaction:" + hashlib.sha256(body).hexdigest()
            responses.append(
                await client.post(
                    INTERACTIVITY, content=body, headers=_forwarded(body, key, form_type)
                )
            )

    assert [response.json() for response in responses] == [
        {"accepted": False},
        {"accepted": True},
        {"accepted": False, "duplicate": True},
        {"accepted": False},
    ]
    assert resume_spy == [{"id": run.id, "action": "approve", "answers": {}, "note": ""}]

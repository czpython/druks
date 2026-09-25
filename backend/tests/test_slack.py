import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import parse_qsl, urlparse

import httpx
import pytest
from conftest import bind_ambient_session, connect_service
from druks.accounts.models import Account
from druks.chat.channels.slack.channel import SlackChannel
from druks.chat.channels.slack.constants import LINK_KEY
from druks.chat.enums import ConversationSource, MessageRole
from druks.chat.models import Conversation
from druks.core.apis.slack import SlackClient
from druks.core.services import Slack
from druks.core.webhooks.slack import SlackEvents
from druks.redis import close_client, get_client
from druks.secrets.datastructures import Audience
from druks.secrets.models import VaultSecret
from druks.testing import configure_app_for_test, make_settings
from fastapi.testclient import TestClient

SLACK_AUDIENCE = Audience.service("slack")
AUTHORITY = "https://slack.com/T1"
ANA = "U100"
BOT = "U900"


@pytest.fixture
async def card(druks_db):
    bind_ambient_session(druks_db)
    card = await connect_service(
        "slack",
        identity={
            "client_id": "c1",
            "team": "Acme",
            "team_id": "T1",
            "bot_name": "druks",
            "bot_user_id": BOT,
        },
        secrets={"client_secret": "cs", "signing_secret": "ss", "bot_token": "xoxb-1"},
    )
    await druks_db.commit()
    return card


@pytest.fixture
def slack(monkeypatch):
    """Slack's Web API, answered locally. Records each call's method and arguments."""
    calls = []

    async def api_call(client, api_method, *, params=None, json=None, **request):
        calls.append((api_method, json or params or {}))
        return {"ok": True, "channel": "D1", "ts": f"{len(calls)}.0"}

    monkeypatch.setattr(SlackClient, "api_call", api_call)
    return calls


@pytest.fixture
def delivery(monkeypatch):
    started = AsyncMock()
    monkeypatch.setattr("druks.chat.channels.slack.channel.DBOS.start_workflow_async", started)
    return started


@pytest.fixture
def endpoint(monkeypatch):
    settings = SimpleNamespace(urls=SimpleNamespace(endpoint="https://druks.example"))
    monkeypatch.setattr("druks.chat.channels.slack.channel.load_settings", lambda: settings)


async def link_person(session, account, user_id=ANA):
    grant = await VaultSecret.connect(
        session,
        SLACK_AUDIENCE,
        account_id=account.id,
        refresh_token="xoxe-1",
        scopes=["users:read"],
        identity={"authority": AUTHORITY, "subject": user_id, "name": "ana"},
    )
    await session.commit()
    return grant


def message_event(user=ANA, text="Read my runs", ts="1.0", **event):
    return {
        "type": "event_callback",
        "team_id": "T1",
        "event": {
            "type": "message",
            "channel": "D1",
            "channel_type": "im",
            "user": user,
            "text": text,
            "ts": ts,
            **event,
        },
    }


async def receive(card, data):
    webhook = SlackEvents(None, {}, None)
    webhook.raw_body = json.dumps(data).encode()
    webhook.card = card
    await webhook.on_event_callback()


async def test_a_linked_persons_message_reaches_their_own_conversation(
    card, druks_db, slack, delivery
):
    ana = await Account.get_or_create(druks_db, "ana@example.com")
    await link_person(druks_db, ana)

    await receive(card, message_event())

    [conversation] = await Conversation.list_for_account(druks_db, ana.id)
    assert (conversation.source, conversation.connection_id, conversation.user_id) == (
        ConversationSource.SLACK,
        card.id,
        ANA,
    )
    await druks_db.refresh(conversation, ["messages"])
    assert [(message.body, message.source_id) for message in conversation.messages] == [
        ("Read my runs", "D1:1.0")
    ]
    delivery.assert_awaited_once()
    assert not slack


@pytest.mark.parametrize(
    "event", [{"user": BOT}, {"subtype": "message_changed"}, {"user_team": "T2"}]
)
async def test_the_webhook_drops_what_is_not_for_the_bot(card, druks_db, slack, delivery, event):
    ana = await Account.get_or_create(druks_db, "ana@example.com")
    await link_person(druks_db, ana)

    await receive(card, message_event(**event))

    assert not await Conversation.list_for_account(druks_db, ana.id)
    assert not slack
    delivery.assert_not_awaited()


async def test_an_unlinked_person_gets_a_link_that_answers_their_message_once_they_connect(
    card, druks_db, druks_client, slack, delivery, endpoint
):
    await receive(card, message_event())

    [(method, sent)] = slack
    assert (method, sent["channel"]) == ("chat.postMessage", ANA)
    link = sent["text"].partition("https://druks.example")[2]
    assert not await Conversation.list_for_connection(druks_db, card.id)

    not_linked = await druks_client.get(link, follow_redirects=False)
    assert not_linked.status_code == 307
    assert not_linked.headers["location"].startswith("/api/oauth/slack/connect?next=")

    operator = await Account.get_or_create(druks_db, "op@example.com")
    await link_person(druks_db, operator)
    linked = await druks_client.get(link, follow_redirects=False)
    [conversation] = await Conversation.list_for_account(druks_db, operator.id)
    assert linked.headers["location"] == f"/chat/{conversation.id}"
    await druks_db.refresh(conversation, ["messages"])
    assert [message.source_id for message in conversation.messages] == ["D1:1.0"]
    delivery.assert_awaited_once()
    assert (await druks_client.get(link, follow_redirects=False)).status_code == 410


async def test_a_forwarded_link_links_only_the_person_who_opens_it(
    card, druks_db, druks_client, slack, delivery, endpoint
):
    await receive(card, message_event())
    [(_, sent)] = slack
    link = sent["text"].partition("https://druks.example")[2]
    operator = await Account.get_or_create(druks_db, "op@example.com")
    await link_person(druks_db, operator, user_id="U200")

    response = await druks_client.get(f"{link}?has_connected=true", follow_redirects=False)

    assert response.headers["location"] == "/chat"
    assert not await Conversation.list_for_connection(druks_db, card.id)
    delivery.assert_not_awaited()
    assert await get_client().get(LINK_KEY.format(token=link.rpartition("/")[2]))


async def test_a_reply_posts_markdown_in_pieces_and_records_its_slack_id(card, druks_db, slack):
    ana = await Account.get_or_create(druks_db, "ana@example.com")
    conversation = await Conversation.get_or_create_for_user(
        druks_db,
        card,
        ana.id,
        source=ConversationSource.SLACK,
        user_id=ANA,
        user_name="",
        user_phone="",
        thread_id="",
    )
    reply = await conversation.create_message(druks_db, "a" * 12_001, role=MessageRole.ASSISTANT)

    await SlackChannel.send_reply(druks_db, conversation, reply)

    assert [
        (method, sent["channel"], len(sent["text"]), sent["blocks"][0]["type"])
        for method, sent in slack
    ] == [("chat.postMessage", ANA, 12_000, "markdown"), ("chat.postMessage", ANA, 1, "markdown")]
    assert reply.source_id == "D1:1.0"


@pytest.fixture
def slack_oauth(monkeypatch):
    """Slack's token endpoint and ``auth.test``, answered locally for Ana's consent."""

    def token_endpoint(request: httpx.Request) -> httpx.Response:
        assert dict(parse_qsl(request.content.decode()))["client_id"] == "c1"
        authed_user = {"id": ANA, "scope": "users:read", "access_token": "xoxp-1"}
        return httpx.Response(
            200, json={"ok": True, "access_token": "xoxb-1", "authed_user": authed_user}
        )

    async def auth_test(client):
        assert client.token == "xoxp-1"
        return {"ok": True, "team_id": "T1", "user_id": ANA, "user": "ana", "team": "Acme"}

    monkeypatch.setattr(
        "druks.services.oauth._http",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(token_endpoint)),
    )
    monkeypatch.setattr(SlackClient, "auth_test", auth_test)


async def test_connect_slack_asks_for_user_scopes_and_keeps_the_token_from_authed_user(
    card, druks_db, tmp_path, slack_oauth
):
    await close_client()
    settings = make_settings(tmp_path, urls={"endpoint": "https://druks.example"})
    with TestClient(configure_app_for_test(settings=settings)) as client:
        consent = client.get("/api/oauth/slack/connect", follow_redirects=False).headers["location"]
        query = dict(parse_qsl(urlparse(consent).query))
        assert (query["user_scope"], "scope" in query) == ("users:read", False)
        page = client.get("/api/oauth/callback", params={"state": query["state"], "code": "c-1"})
        assert page.status_code == 200

    operator = await Account.get_or_create(druks_db, "op@example.com")
    assert await Account.lookup(druks_db, AUTHORITY, ANA) == operator
    [grant] = await VaultSecret.list_account_connections(druks_db, SLACK_AUDIENCE, operator.id)
    assert (grant.identity, grant.scopes) == (
        {"authority": AUTHORITY, "subject": ANA, "name": "ana"},
        ["users:read"],
    )
    client = await Slack.get_oauth_client()
    assert await client.get_access_token(druks_db, connection=grant) == ("xoxp-1", None)

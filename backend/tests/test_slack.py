import json
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import parse_qsl, urlparse

import druks.chat.channels.slack.subscribers  # noqa: F401 — the webhook publishes to it
import httpx
import pytest
from conftest import bind_ambient_session, connect_service
from druks.accounts.models import Account, PersonalAccessToken
from druks.api.dependencies import request_session
from druks.chat.channels.slack.channel import SlackChannel
from druks.chat.channels.slack.constants import LINK_KEY
from druks.chat.constants import CONVERSATION_HEADER
from druks.chat.enums import ConversationSource, MessageRole
from druks.chat.exceptions import ChannelHasNoThreadsError
from druks.chat.models import Conversation
from druks.chat.routes import read_thread
from druks.core.apis.slack import SlackClient
from druks.core.services import Slack
from druks.core.webhooks.slack import SlackEvents
from druks.models import Base
from druks.redis import close_client, get_client
from druks.secrets.datastructures import Audience
from druks.secrets.models import VaultSecret
from druks.testing import asgi_client, configure_app_for_test, make_settings
from fastapi import Depends, FastAPI
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


def message_event(
    user=ANA, text="Read my runs", ts="1.0", channel="D1", channel_type="im", **event
):
    return {
        "type": "event_callback",
        "team_id": "T1",
        "event": {
            "type": "message",
            "channel": channel,
            "channel_type": channel_type,
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


def test_create_slack_app_opens_slack_with_the_whole_manifest(tmp_path):
    settings = make_settings(tmp_path, urls={"endpoint": "https://druks.example"})
    with TestClient(configure_app_for_test(settings=settings)) as client:
        response = client.get("/api/core/services/slack/manifest", follow_redirects=False)
        manifest = Slack.get_manifest()

    location = urlparse(response.headers["location"])
    query = dict(parse_qsl(location.query))
    assert (location.netloc, location.path, query["new_app"]) == ("api.slack.com", "/apps", "1")
    assert json.loads(query["manifest_json"]) == manifest


def room_event(user=ANA, text=f"<@{BOT}> read my runs", ts="10.0", **event):
    return message_event(user, text, ts, channel="C1", channel_type="channel", **event)


class Answer(dict):
    """One page of a Slack answer, which iterates over itself as the SDK's does."""

    def __aiter__(self):
        return self.pages()

    async def pages(self):
        yield self


def thread_reads(monkeypatch, messages):
    """``conversations.replies`` and ``users.info``, answered locally. Records each read."""
    names = {ANA: "Ana", BOT: "Druks", "U200": "Ben"}
    reads = []

    async def api_call(client, api_method, *, params=None, json=None, **request):
        reads.append(api_method)
        if api_method == "conversations.replies":
            return Answer(ok=True, messages=messages)
        user = params["user"]
        profile = {"display_name": ""}
        return Answer(ok=True, user={"id": user, "real_name": names[user], "profile": profile})

    monkeypatch.setattr(SlackClient, "api_call", api_call)
    return reads


async def thread_conversation(session, card, account, *, thread_id="C1:10.0"):
    return await Conversation.get_or_create_for_user(
        session,
        card,
        account.id,
        source=ConversationSource.SLACK,
        user_id=ANA,
        user_name="",
        user_phone="",
        thread_id=thread_id,
    )


async def test_each_person_who_tags_the_bot_gets_their_own_conversation_for_the_thread(
    card, druks_db, slack, delivery
):
    ana = await Account.get_or_create(druks_db, "ana@example.com")
    ben = await Account.get_or_create(druks_db, "ben@example.com")
    await link_person(druks_db, ana)
    await link_person(druks_db, ben, user_id="U200")

    await receive(card, room_event())
    await receive(card, room_event(user="U200", ts="11.0", thread_ts="10.0"))

    conversations = await Conversation.list_for_connection(druks_db, card.id)
    assert sorted((chat.account_id, chat.user_id, chat.thread_id) for chat in conversations) == (
        sorted([(ana.id, ANA, "C1:10.0"), (ben.id, "U200", "C1:10.0")])
    )
    assert delivery.await_count == 2
    reply = await conversations[0].create_message(druks_db, "Done.", role=MessageRole.ASSISTANT)
    await SlackChannel.send_reply(druks_db, conversations[0], reply)
    [(method, sent)] = slack
    assert (method, sent["channel"], sent["thread_ts"]) == ("chat.postMessage", "C1", "10.0")


async def test_only_a_linked_persons_untagged_reply_in_a_fresh_thread_reaches_their_agent(
    card, druks_db, slack, delivery
):
    ana = await Account.get_or_create(druks_db, "ana@example.com")
    ben = await Account.get_or_create(druks_db, "ben@example.com")
    await link_person(druks_db, ana)
    await link_person(druks_db, ben, user_id="U200")
    await receive(card, room_event())

    await receive(card, room_event(text="more", ts="12.0", thread_ts="10.0"))
    await receive(card, room_event(user="U200", text="me too", ts="13.0", thread_ts="10.0"))
    await receive(card, room_event(text="a new topic", ts="14.0"))

    [conversation] = await Conversation.list_for_connection(druks_db, card.id)
    await druks_db.refresh(conversation, ["messages"])
    assert [message.body for message in conversation.messages] == ["read my runs", "more"]
    assert delivery.await_count == 2
    assert not slack

    a_day_ago = Base.utc_now() - timedelta(hours=25)
    for message in conversation.messages:
        message.created_at = a_day_ago
    conversation.created_at = a_day_ago
    await druks_db.commit()
    await druks_db.refresh(conversation)
    await receive(card, room_event(text="still there?", ts="15.0", thread_ts="10.0"))

    await druks_db.refresh(conversation, ["messages"])
    assert len(conversation.messages) == 2


async def test_an_unlinked_person_who_tags_the_bot_gets_the_link_where_only_they_see_it(
    card, druks_db, slack, delivery, endpoint
):
    await receive(card, room_event(ts="10.0"))
    await receive(card, room_event(text="hello?", ts="11.0", thread_ts="10.0"))

    [(method, sent)] = slack
    assert (method, sent["channel"], sent["user"]) == ("chat.postEphemeral", "C1", ANA)
    assert "/api/chat/services/slack/link/" in sent["text"]
    delivery.assert_not_awaited()


async def test_the_agent_reads_its_thread_and_knows_who_wrote_what(card, druks_db, monkeypatch):
    """Ben's agent answers in the same thread as the same bot: only the replies this
    conversation recorded are the agent's own."""
    ana = await Account.get_or_create(druks_db, "ana@example.com")
    reads = thread_reads(
        monkeypatch,
        [
            {"ts": "10.0", "user": ANA, "text": f"<@{BOT}> read my runs"},
            {"ts": "10.1", "user": BOT, "text": "Sure."},
            {"ts": "10.2", "user": "U200", "text": "me too"},
            {"ts": "10.3", "user": BOT, "text": "Hi Ben."},
        ],
    )
    conversation = await thread_conversation(druks_db, card, ana)
    await conversation.create_message(
        druks_db, "Sure.", role=MessageRole.ASSISTANT, source_id="C1:10.1"
    )

    thread = await SlackChannel.read_thread(druks_db, conversation)
    await SlackChannel.read_thread(druks_db, conversation)

    assert [
        (message["ts"], message["user_name"], message["is_from_you"], message["is_from_user"])
        for message in thread
    ] == [
        ("10.0", "Ana", False, True),
        ("10.1", "Druks", True, False),
        ("10.2", "Ben", False, False),
        ("10.3", "Druks", False, False),
    ]
    assert reads.count("users.info") == 3
    direct = await thread_conversation(druks_db, card, ana, thread_id="")
    with pytest.raises(ChannelHasNoThreadsError):
        await SlackChannel.read_thread(druks_db, direct)


async def test_chat_read_thread_reads_the_callers_own_conversation(
    card, druks_db, tmp_path, monkeypatch
):
    configure_app_for_test(settings=make_settings(tmp_path), authenticated=False)
    api = FastAPI(dependencies=[Depends(request_session)])
    api.add_api_route("/thread", read_thread, methods=["GET"])
    ana = await Account.get_or_create(druks_db, "ana@example.com")
    thread_reads(monkeypatch, [{"ts": "10.0", "user": ANA, "text": "hi"}])
    conversation = await thread_conversation(druks_db, card, ana)
    direct = await thread_conversation(druks_db, card, ana, thread_id="")
    _, token = await PersonalAccessToken.create(druks_db, account_id=ana.id, name="chat")
    await druks_db.commit()
    headers = {"Authorization": f"Bearer {token}"}

    async with asgi_client(api) as client:
        outside = await client.get("/thread", headers=headers)
        thread = await client.get(
            "/thread", headers={**headers, CONVERSATION_HEADER: conversation.id}
        )
        no_thread = await client.get("/thread", headers={**headers, CONVERSATION_HEADER: direct.id})

    assert (outside.status_code, no_thread.status_code) == (409, 409)
    assert [(message["ts"], message["is_from_user"]) for message in thread.json()] == [
        ("10.0", True)
    ]


def shared_file(file_id="F1", name="plan.pdf"):
    return {
        "id": file_id,
        "name": name,
        "mimetype": "application/pdf",
        "url_private_download": f"https://files.slack.com/{file_id}/{name}",
    }


async def test_a_file_a_linked_person_sends_is_a_druks_file_on_their_message(
    card, druks_db, slack, delivery, tmp_path, monkeypatch
):
    monkeypatch.setenv("DRUKS_DATA_DIR", str(tmp_path))
    downloads = []

    async def download(client, url):
        downloads.append((client.token, url))
        return b"%PDF"

    monkeypatch.setattr(SlackClient, "download", download)
    ana = await Account.get_or_create(druks_db, "ana@example.com")
    await link_person(druks_db, ana)

    await receive(card, message_event(text="", subtype="file_share", files=[shared_file()]))
    await receive(
        card,
        message_event(
            text="both of these",
            ts="2.0",
            subtype="file_share",
            files=[shared_file("F2", "a.pdf"), shared_file("F3", "b.pdf")],
        ),
    )
    await receive(card, message_event(text="", ts="3.0"))

    [conversation] = await Conversation.list_for_account(druks_db, ana.id)
    await druks_db.refresh(conversation, ["messages"])
    assert [
        (message.body, message.source_id, message.file.name) for message in conversation.messages
    ] == [
        ("plan.pdf", "D1:1.0", "plan.pdf"),
        ("both of these", "D1:2.0", "a.pdf"),
        ("b.pdf", "D1:2.0:F3", "b.pdf"),
    ]
    assert [url for _, url in downloads] == [
        "https://files.slack.com/F1/plan.pdf",
        "https://files.slack.com/F2/a.pdf",
        "https://files.slack.com/F3/b.pdf",
    ]
    assert {token for token, _ in downloads} == {"xoxb-1"}
    assert delivery.await_count == 2

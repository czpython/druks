from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import parse_qsl, urlparse

import druks.chat.channels.github.subscribers  # noqa: F401 — the webhook publishes to it
import httpx
import pytest
from conftest import bind_ambient_session, connect_service
from druks.accounts.models import Account
from druks.chat.channels.github.channel import GitHubChannel
from druks.chat.enums import ConversationSource, MessageRole
from druks.chat.models import Conversation
from druks.core.apis.github import GITHUB_AUTHORITY, GitHubClient
from druks.core.services import Github
from druks.core.webhooks import github as github_webhooks
from druks.core.webhooks.github import GitHubEvents
from druks.redis import close_client
from druks.secrets.datastructures import Audience
from druks.secrets.models import VaultSecret
from druks.testing import configure_app_for_test, make_settings
from fastapi.testclient import TestClient
from githubkit import GitHub

GITHUB_AUDIENCE = Audience.service("github")
PEM = "-----BEGIN RSA PRIVATE KEY-----\nline-one\n-----END RSA PRIVATE KEY-----\n"
HANDLE = "druks-acme"
ANA = 100
BEN = 200
THREAD = "acme/app#7"


@pytest.fixture
async def card(druks_db):
    bind_ambient_session(druks_db)
    card = await connect_service(
        "github",
        identity={"app_id": "1", "slug": HANDLE, "client_id": "c1"},
        secrets={"client_secret": "cs", "private_key": PEM, "webhook_secret": "ws"},
    )
    await druks_db.commit()
    return card


@pytest.fixture
def github(monkeypatch):
    """GitHub's REST API, answered locally. Records each comment Druks posts."""
    posts = []

    async def create_comment(client, repo, number, body):
        posts.append(("create_comment", repo, number, body))
        return {"id": 900 + len(posts)}

    async def reply_to_review_comment(client, repo, number, comment_id, body):
        posts.append(("reply_to_review_comment", repo, number, comment_id, body))
        return {"id": 900 + len(posts)}

    monkeypatch.setattr(GitHubClient, "create_comment", create_comment)
    monkeypatch.setattr(GitHubClient, "reply_to_review_comment", reply_to_review_comment)
    return posts


@pytest.fixture
def delivery(monkeypatch):
    started = AsyncMock()
    monkeypatch.setattr("druks.chat.channels.github.channel.DBOS.start_workflow_async", started)
    return started


async def link_person(session, account, user_id=ANA, login="ana"):
    grant = await VaultSecret.connect(
        session,
        GITHUB_AUDIENCE,
        account_id=account.id,
        refresh_token="ghr-1",
        scopes=[],
        identity={"authority": GITHUB_AUTHORITY, "subject": str(user_id), "login": login},
    )
    await session.commit()
    return grant


def comment_event(
    *,
    user=ANA,
    login="ana",
    body=f"@{HANDLE} read my runs",
    comment_id=1,
    association="MEMBER",
    sender_type="User",
    is_pull_request=False,
    in_reply_to_id=None,
):
    """A comment delivery. ``in_reply_to_id`` makes it an inline reply; ``comment_id``
    alone with ``is_pull_request`` and no thread stays a top-level comment."""
    comment = {"id": comment_id, "body": body, "author_association": association}
    if in_reply_to_id:
        comment["in_reply_to_id"] = in_reply_to_id
    return {
        "repository": {"full_name": "acme/app"},
        "issue": {"number": 7, **({"pull_request": {}} if is_pull_request else {})},
        "pull_request": {"number": 7},
        "sender": {"type": sender_type, "login": login, "id": user},
        "comment": comment,
    }


async def receive(tmp_path, data, *, inline=False):
    events = GitHubEvents(request=SimpleNamespace(), kwargs={}, settings=make_settings(tmp_path))
    events._data_cached = data
    if inline:
        await events.on_pull_request_review_comment_created()
    else:
        await events.on_issue_comment_created()


async def test_the_webhook_publishes_every_persons_comment_as_one_fact(tmp_path, monkeypatch):
    published = []

    async def publish(name, **kwargs):
        published.append((name, kwargs))

    monkeypatch.setattr(github_webhooks, "publish", publish)

    await receive(tmp_path, comment_event())
    await receive(tmp_path, comment_event(comment_id=2, is_pull_request=True))
    await receive(tmp_path, comment_event(comment_id=50, association="NONE"), inline=True)
    await receive(tmp_path, comment_event(comment_id=51, in_reply_to_id=50), inline=True)
    await receive(tmp_path, comment_event(comment_id=3, sender_type="Bot"))

    assert [name for name, _ in published] == ["issue.commented"] * 4
    assert [(facts["repo"], facts["number"]) for _, facts in published] == [("acme/app", 7)] * 4
    assert [
        (
            payload["comment_id"],
            payload["review_thread_id"],
            payload["is_pull_request"],
            payload["author_can_write"],
        )
        for _, facts in published
        for payload in [facts["payload"]]
    ] == [
        (1, None, False, True),
        (2, None, True, True),
        (50, 50, True, False),
        (51, 50, True, True),
    ]
    assert published[0][1]["payload"]["author"] == "ana"
    assert published[0][1]["payload"]["author_id"] == ANA
    assert published[0][1]["payload"]["body"] == f"@{HANDLE} read my runs"


async def test_a_linked_writers_tag_reaches_their_own_conversation(
    card, druks_db, tmp_path, github, delivery
):
    ana = await Account.get_or_create(druks_db, "ana@example.com")
    await link_person(druks_db, ana)

    await receive(tmp_path, comment_event(body=f"Hey @{HANDLE}, what changed?"))

    [conversation] = await Conversation.list_for_account(druks_db, ana.id)
    assert (
        conversation.source,
        conversation.connection_id,
        conversation.user_id,
        conversation.user_name,
        conversation.thread_id,
        conversation.title,
    ) == (ConversationSource.GITHUB, card.id, str(ANA), "ana", THREAD, THREAD)
    await druks_db.refresh(conversation, ["messages"])
    assert [(message.body, message.source_id) for message in conversation.messages] == [
        (f"Hey @{HANDLE}, what changed?", "issue_comment:1")
    ]
    delivery.assert_awaited_once()
    assert not github


@pytest.mark.parametrize(
    "event",
    [
        {"association": "NONE"},
        {"body": "no tag here"},
        {"body": f"> @{HANDLE} said this\n\nfine by me"},
        {"body": f"mail ops@{HANDLE}.dev or ask @{HANDLE}-team"},
    ],
)
async def test_the_channel_drops_what_is_not_for_the_bot(
    card, druks_db, tmp_path, github, delivery, event
):
    ana = await Account.get_or_create(druks_db, "ana@example.com")
    await link_person(druks_db, ana)

    await receive(tmp_path, comment_event(**event))

    assert not await Conversation.list_for_account(druks_db, ana.id)
    assert not github
    delivery.assert_not_awaited()


async def test_a_comment_delivered_twice_is_saved_once(card, druks_db, tmp_path, github, delivery):
    ana = await Account.get_or_create(druks_db, "ana@example.com")
    await link_person(druks_db, ana)

    await receive(tmp_path, comment_event())
    await receive(tmp_path, comment_event())

    [conversation] = await Conversation.list_for_account(druks_db, ana.id)
    await druks_db.refresh(conversation, ["messages"])
    assert len(conversation.messages) == 1
    delivery.assert_awaited_once()


async def test_each_writer_who_tags_the_bot_gets_their_own_conversation_for_the_issue(
    card, druks_db, tmp_path, github, delivery
):
    ana = await Account.get_or_create(druks_db, "ana@example.com")
    ben = await Account.get_or_create(druks_db, "ben@example.com")
    await link_person(druks_db, ana)
    await link_person(druks_db, ben, user_id=BEN, login="ben")

    await receive(tmp_path, comment_event())
    await receive(tmp_path, comment_event(user=BEN, login="ben", comment_id=2))

    conversations = await Conversation.list_for_connection(druks_db, card.id)
    assert sorted((chat.account_id, chat.user_id, chat.thread_id) for chat in conversations) == (
        sorted([(ana.id, str(ANA), THREAD), (ben.id, str(BEN), THREAD)])
    )
    assert delivery.await_count == 2


async def test_an_unlinked_writer_is_told_where_to_connect(
    card, druks_db, tmp_path, github, delivery
):
    await receive(tmp_path, comment_event(comment_id=50, association="OWNER"), inline=True)

    [(method, repo, number, thread, text)] = github
    assert (method, repo, number, thread) == ("reply_to_review_comment", "acme/app", 7, 50)
    assert text.startswith("@ana ")
    assert not await Conversation.list_for_connection(druks_db, card.id)
    delivery.assert_not_awaited()


async def issue_conversation(session, card, account):
    return await Conversation.get_or_create_for_user(
        session,
        card,
        account.id,
        source=ConversationSource.GITHUB,
        user_id=str(ANA),
        user_name="ana",
        user_phone="",
        thread_id=THREAD,
    )


async def test_a_reply_goes_where_the_tag_was(card, druks_db, github):
    ana = await Account.get_or_create(druks_db, "ana@example.com")
    conversation = await issue_conversation(druks_db, card, ana)
    top = await conversation.create_message(
        druks_db, f"@{HANDLE} and?", source_id="issue_comment:2"
    )
    answered = await conversation.create_message(
        druks_db, "Then this.", role=MessageRole.ASSISTANT, reply_to=top
    )
    await GitHubChannel.send_reply(druks_db, conversation, answered)
    inline = await conversation.create_message(
        druks_db, f"@{HANDLE} why?", source_id="review_comment:50:51"
    )
    internal = await conversation.create_message(druks_db, "[Internal: ...]", is_internal=True)
    # A tag on another line arrives while the turn about line 50 still runs.
    later = await conversation.create_message(
        druks_db, f"@{HANDLE} and here?", source_id="review_comment:70:71"
    )
    replies = [
        await conversation.create_message(
            druks_db, text, role=MessageRole.ASSISTANT, reply_to=asked
        )
        for asked, text in [(inline, "Because."), (internal, "It failed."), (later, "Same.")]
    ]

    for reply in replies:
        await GitHubChannel.send_reply(druks_db, conversation, reply)

    assert github == [
        ("create_comment", "acme/app", 7, "Then this."),
        ("reply_to_review_comment", "acme/app", 7, 50, "Because."),
        ("reply_to_review_comment", "acme/app", 7, 50, "It failed."),
        ("reply_to_review_comment", "acme/app", 7, 70, "Same."),
    ]
    assert [reply.source_id for reply in (answered, *replies)] == [
        "issue_comment:901",
        "review_comment:50:902",
        "review_comment:50:903",
        "review_comment:70:904",
    ]


def thread_reads(monkeypatch, *, comments, review_comments, is_pull_request=True):
    """The issue and its comments, answered locally. Records each read."""
    reads = []

    async def get_issue(client, repo, number):
        reads.append("get_issue")
        return {
            "title": "Slow start",
            "body": "It takes a minute.",
            "user": {"id": BEN, "login": "ben"},
            "created_at": datetime(2026, 9, 25, 9, 0, tzinfo=UTC),
            "pull_request": {"url": "https://api.github.com/x"} if is_pull_request else None,
        }

    async def list_comments(client, repo, number):
        reads.append("list_comments")
        return comments

    async def list_review_comments(client, repo, number):
        reads.append("list_review_comments")
        return review_comments

    monkeypatch.setattr(GitHubClient, "get_issue", get_issue)
    monkeypatch.setattr(GitHubClient, "list_comments", list_comments)
    monkeypatch.setattr(GitHubClient, "list_review_comments", list_review_comments)
    return reads


def posted(comment_id, user, login, body, minute, **inline):
    return {
        "id": comment_id,
        "user": {"id": user, "login": login},
        "body": body,
        "created_at": datetime(2026, 9, 25, 9, minute, tzinfo=UTC),
        **inline,
    }


async def test_the_agent_reads_the_issue_and_its_comments_and_knows_who_wrote_what(
    card, druks_db, monkeypatch
):
    """Ben's agent answers in the same thread as the same App: only the replies this
    conversation recorded are the agent's own."""
    ana = await Account.get_or_create(druks_db, "ana@example.com")
    bot = 999
    reads = thread_reads(
        monkeypatch,
        comments=[
            posted(1, ANA, "ana", f"@{HANDLE} read my runs", 1),
            posted(2, bot, f"{HANDLE}[bot]", "Sure.", 3),
            posted(3, bot, f"{HANDLE}[bot]", "Hi Ben.", 5),
        ],
        review_comments=[
            posted(50, BEN, "ben", "why here?", 2, path="app.py", line=12, in_reply_to_id=None),
            posted(
                51, bot, f"{HANDLE}[bot]", "Because.", 4, path="app.py", line=12, in_reply_to_id=50
            ),
        ],
    )
    conversation = await issue_conversation(druks_db, card, ana)
    await conversation.create_message(
        druks_db, "Sure.", role=MessageRole.ASSISTANT, source_id="issue_comment:2"
    )

    thread = await GitHubChannel.read_thread(druks_db, conversation)

    assert [
        (entry["user_name"], entry["text"], entry["is_from_you"], entry["is_from_user"])
        for entry in thread
    ] == [
        ("ben", "Slow start\n\nIt takes a minute.", False, False),
        ("ana", f"@{HANDLE} read my runs", False, True),
        ("ben", "why here?", False, False),
        (f"{HANDLE}[bot]", "Sure.", True, False),
        (f"{HANDLE}[bot]", "Because.", False, False),
        (f"{HANDLE}[bot]", "Hi Ben.", False, False),
    ]
    assert [(entry["path"], entry["line"]) for entry in thread] == [
        ("", None),
        ("", None),
        ("app.py", 12),
        ("", None),
        ("app.py", 12),
        ("", None),
    ]
    assert thread[0]["ts"] == "2026-09-25T09:00:00+00:00"
    assert reads == ["get_issue", "list_comments", "list_review_comments"]


async def test_a_plain_issue_has_no_inline_comments_to_read(card, druks_db, monkeypatch):
    ana = await Account.get_or_create(druks_db, "ana@example.com")
    reads = thread_reads(monkeypatch, comments=[], review_comments=[], is_pull_request=False)
    conversation = await issue_conversation(druks_db, card, ana)

    thread = await GitHubChannel.read_thread(druks_db, conversation)

    assert [entry["text"] for entry in thread] == ["Slow start\n\nIt takes a minute."]
    assert reads == ["get_issue", "list_comments"]


@pytest.fixture
def github_oauth(monkeypatch):
    """GitHub's token endpoint and the signed-in user, answered locally for Ana's consent."""

    def token_endpoint(request: httpx.Request) -> httpx.Response:
        sent = dict(parse_qsl(request.content.decode()))
        assert (sent["client_id"], request.headers["accept"]) == ("c1", "application/json")
        tokens = {"access_token": "ghu-1", "refresh_token": "ghr-1", "expires_in": 28800}
        return httpx.Response(200, json={**tokens, "token_type": "bearer", "scope": ""})

    async def get_authenticated(client):
        assert client._github.auth.token == "ghu-1"
        return SimpleNamespace(parsed_data=SimpleNamespace(id=ANA, login="ana"))

    monkeypatch.setattr(
        "druks.services.oauth._http",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(token_endpoint)),
    )
    github = GitHub("x")
    monkeypatch.setattr(type(github.rest.users), "async_get_authenticated", get_authenticated)


async def test_connect_github_links_the_signed_in_login_to_the_account(
    card, druks_db, tmp_path, github_oauth
):
    await close_client()
    settings = make_settings(tmp_path, urls={"endpoint": "https://druks.example"})
    with TestClient(configure_app_for_test(settings=settings)) as client:
        door = client.get("/api/oauth/github/connect", follow_redirects=False)
        consent = door.headers["location"]
        assert consent.startswith("https://github.com/login/oauth/authorize?")
        query = dict(parse_qsl(urlparse(consent).query))
        assert (query["client_id"], "scope" in query) == ("c1", False)
        page = client.get("/api/oauth/callback", params={"state": query["state"], "code": "c-1"})
        assert page.status_code == 200

    operator = await Account.get_or_create(druks_db, "op@example.com")
    assert await Account.lookup(druks_db, GITHUB_AUTHORITY, str(ANA)) == operator
    [grant] = await VaultSecret.list_account_connections(druks_db, GITHUB_AUDIENCE, operator.id)
    assert grant.identity == {"authority": GITHUB_AUTHORITY, "subject": str(ANA), "login": "ana"}
    client = await Github.get_oauth_client()
    token, expires_at = await client.get_access_token(druks_db, connection=grant)
    assert (token, bool(expires_at)) == ("ghu-1", True)

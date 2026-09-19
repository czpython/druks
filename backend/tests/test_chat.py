import asyncio
import hashlib
from contextlib import asynccontextmanager
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from druks.accounts.models import Account, PersonalAccessToken
from druks.chat import routes, service
from druks.chat.bridge import Bridge
from druks.chat.constants import CHAT_KEY_NAME
from druks.chat.enums import ConversationSource, MessageRole, MessageState
from druks.chat.exceptions import ChatBridgeError, ChatSandboxGone
from druks.chat.models import Conversation, Message
from druks.database import get_session
from druks.files.datastructures import File
from druks.files.models import FileRecord
from druks.harnesses.claude import ClaudeHarness
from druks.mcp.inbound import get_druks_account_token, get_druks_mcp_server
from druks.models import Base
from druks.redis import get_client
from druks.sandbox.exceptions import IdentityDenied
from druks.sandbox.models import SandboxIdentity, SecretRef
from druks.testing import asgi_client, configure_app_for_test, make_settings
from fastapi import WebSocket, WebSocketDisconnect
from sqlalchemy import delete


@pytest.fixture
async def conversation(druks_db, monkeypatch):
    monkeypatch.setattr(
        "druks.mcp.inbound.load_settings",
        lambda: SimpleNamespace(
            urls=SimpleNamespace(webhook_host="hooks.example.com", endpoint="")
        ),
    )
    account = await Account.get_or_create(druks_db, "owner@example.com")
    return await Conversation.create(druks_db, account_id=account.id, body="Find the issue")


async def list_messages(session, conversation):
    await session.refresh(conversation, ["messages"])
    return conversation.messages


@pytest.fixture
async def sandbox(druks_db, conversation, monkeypatch):
    identity, _ = await SandboxIdentity.create(
        druks_db, account_id=conversation.account_id, run_id=None, scoped_to="chat", secret_refs=[]
    )
    await identity.bind("chat-sandbox")
    host = SimpleNamespace(
        id="chat-sandbox",
        ssh_username="druks",
        aclose=AsyncMock(),
        upload_file=AsyncMock(),
        download=AsyncMock(),
        exec=AsyncMock(return_value=SimpleNamespace(ok=True, stdout="Gate check\n")),
    )
    monkeypatch.setattr(service, "get_sandbox", AsyncMock(return_value=(host, identity)))
    monkeypatch.setattr(service, "get_running_sandbox", AsyncMock(return_value=host))
    monkeypatch.setattr(
        service,
        "get_default_config",
        AsyncMock(
            return_value=SimpleNamespace(
                harness_class=ClaudeHarness, model_id="claude-opus-4-7", effort="", fast_mode=False
            )
        ),
    )
    monkeypatch.setattr(service, "sandbox_client", SimpleNamespace(set_expiry=AsyncMock()))
    return host, identity


async def test_new_conversation_is_unnamed_private_and_answers_in_order(druks_db, conversation):
    other = await Account.get_or_create(druks_db, "other@example.com")
    assert not conversation.title
    assert conversation.source == ConversationSource.WEB
    assert not conversation.session_file
    assert not await Conversation.get_for_account(druks_db, conversation.id, other.id)
    assert not await Conversation.list_for_account(druks_db, other.id)
    first = await conversation.get_unanswered_message(druks_db)
    await conversation.create_message(druks_db, "second")
    assert first.state == MessageState.PENDING
    assert await conversation.get_unanswered_message(druks_db) is first


@pytest.mark.parametrize(
    "webhook_host,expected",
    [
        ("hooks.example.com", "https://hooks.example.com/mcp"),
        ("", "http://127.0.0.1:8000/mcp"),
    ],
)
def test_mcp_address_uses_the_public_host_or_endpoint(monkeypatch, webhook_host, expected):
    monkeypatch.setattr(
        "druks.mcp.inbound.load_settings",
        lambda: SimpleNamespace(
            urls=SimpleNamespace(webhook_host=webhook_host, endpoint="http://127.0.0.1:8000/")
        ),
    )
    assert get_druks_mcp_server(allowed_tools=()).url == expected


async def test_chat_key_is_separate_from_the_build_key(druks_db, conversation):
    build = await get_druks_account_token(
        druks_db, conversation.account_id, ("software_factory_get_ticket",)
    )
    chat = await get_druks_account_token(druks_db, conversation.account_id, (), name=CHAT_KEY_NAME)
    assert build.id != chat.id
    assert (
        await PersonalAccessToken.get_for_prefix(druks_db, build.identity["token_prefix"])
    ).allowed_tools == ["software_factory_get_ticket"]
    assert not (
        await PersonalAccessToken.get_for_prefix(druks_db, chat.identity["token_prefix"])
    ).allowed_tools
    assert (
        await get_druks_account_token(druks_db, conversation.account_id, (), name=CHAT_KEY_NAME)
    ).id == chat.id


async def test_chat_identity_authenticates_until_lease_expiry_and_is_not_an_orphan(
    druks_db, conversation
):
    token = await get_druks_account_token(druks_db, conversation.account_id, (), name=CHAT_KEY_NAME)
    identity, entries = await SandboxIdentity.create(
        druks_db,
        account_id=conversation.account_id,
        run_id=None,
        scoped_to="chat",
        secret_refs=[
            SecretRef(name="mcp_druks_token", secret_id=token.id, host="hooks.example.com")
        ],
    )
    await identity.bind("chat-sandbox")
    bearer = entries["mcp_druks_token"].headers["Authorization"].removeprefix("Bearer ")
    assert await SandboxIdentity.authenticate(druks_db, identity.id, bearer, "mcp_druks_token")
    assert not await SandboxIdentity.list_orphans(druks_db)
    assert await SandboxIdentity.lookup(
        druks_db, account_id=conversation.account_id, run_id=None, scoped_to="chat"
    )
    identity.expires_at = Base.utc_now() - timedelta(seconds=1)
    await druks_db.commit()
    with pytest.raises(IdentityDenied):
        await SandboxIdentity.authenticate(druks_db, identity.id, bearer, "mcp_druks_token")
    assert not await SandboxIdentity.lookup(
        druks_db, account_id=conversation.account_id, run_id=None, scoped_to="chat"
    )


async def test_conversations_share_the_account_sandbox_until_its_secrets_change(
    druks_db, conversation, monkeypatch
):
    hosts = {}

    async def provision(**values):
        host = SimpleNamespace(id=f"sandbox-{len(hosts) + 1}", aclose=AsyncMock())
        hosts[host.id] = host
        await values["identity"].bind(host.id)
        return host

    @asynccontextmanager
    async def attach(*, host_id):
        yield hosts[host_id]

    release = AsyncMock()
    monkeypatch.setattr(
        service,
        "sandbox_client",
        SimpleNamespace(provision=provision, attach=attach, release=release),
    )
    monkeypatch.setattr(service, "get_template_id", AsyncMock(return_value="chat-template"))
    monkeypatch.setattr(Bridge, "start", AsyncMock())
    second = await Conversation.create(
        druks_db, account_id=conversation.account_id, body="another conversation"
    )
    config = SimpleNamespace(secret_refs=[], secrets={})
    first_host, _identity = await service.get_sandbox(druks_db, conversation.account_id, config)
    second_host, _identity = await service.get_sandbox(druks_db, second.account_id, config)
    login = await get_druks_account_token(druks_db, conversation.account_id, (), name="login")
    moved = SimpleNamespace(
        secret_refs=[SecretRef(name="claude_token", secret_id=login.id)], secrets={}
    )
    moved_host, _identity = await service.get_sandbox(druks_db, second.account_id, moved)

    assert first_host.id == second_host.id
    assert moved_host.id != first_host.id
    release.assert_awaited_once_with(host_id=first_host.id)


async def test_delivered_turn_is_never_sent_again_after_a_transport_failure(
    druks_db, conversation, sandbox, monkeypatch
):
    state = {"status": "missing", "sessionId": "", "messageId": ""}
    prompts = []

    async def request(self, method, **values):
        if method == "prompt":
            assert not druks_db.in_transaction()
            prompts.append(values["messageId"])
            raise ChatBridgeError("The prompt response was lost.")
        return state

    monkeypatch.setattr(Bridge, "request", request)
    with pytest.raises(ChatBridgeError):
        await service.deliver_pending(druks_db, conversation)
    [message] = await list_messages(druks_db, conversation)
    assert message.state == MessageState.DELIVERED

    await service.deliver_pending(druks_db, conversation)

    assert prompts == [message.id]
    assert message.state == "interrupted"
    _, identity = sandbox
    assert service.sandbox_client.set_expiry.await_args.kwargs["expires_at"] == identity.expires_at


async def test_missing_sandbox_interrupts_delivered_turn_without_provisioning(
    druks_db, conversation, monkeypatch
):
    [message] = await list_messages(druks_db, conversation)
    message.state = MessageState.DELIVERED
    await druks_db.commit()
    get_running_sandbox = AsyncMock(side_effect=ChatSandboxGone("expired"))
    monkeypatch.setattr(service, "get_running_sandbox", get_running_sandbox)

    await service.deliver_pending(druks_db, conversation)

    assert message.state == "interrupted"
    get_running_sandbox.assert_awaited_once_with(druks_db, conversation.account_id)


@pytest.mark.parametrize("terminal_state", ["replied", "cancelled"])
async def test_recovery_reads_live_events_then_saves_reply_and_replaces_archive(
    druks_db, conversation, sandbox, tmp_path, monkeypatch, terminal_state
):
    monkeypatch.setenv("DRUKS_DATA_DIR", str(tmp_path))
    host, _identity = sandbox
    previous = FileRecord(
        id="previous",
        name="session.tar.gz",
        size=1,
        content_type="application/gzip",
        sha256=hashlib.sha256(b"x").hexdigest(),
        app="chat",
    )
    druks_db.add(previous)
    await druks_db.flush()
    conversation.session_file = File(id=previous.id)
    [message] = await list_messages(druks_db, conversation)
    message.state = MessageState.DELIVERED
    await druks_db.commit()
    updates = [
        {"sessionUpdate": "tool_call_update", "toolCallId": "stopped-turn", "status": "completed"},
        {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "Found it."}},
        {
            "sessionUpdate": "tool_call",
            "toolCallId": "tool-1",
            "title": "Get ticket",
            "status": "in_progress",
            "rawInput": {"run": "run-one"},
        },
        {
            "sessionUpdate": "tool_call_update",
            "toolCallId": "tool-1",
            "status": "completed",
            "rawOutput": {"gate": "approval"},
        },
    ]
    events = [
        {
            "sequence": index,
            "epoch": "sandbox-one",
            "messageId": message.id,
            "notification": {"sessionId": "session-one", "update": update},
        }
        for index, update in enumerate(updates, 1)
    ]
    statuses = 0

    async def request(self, method, **values):
        nonlocal statuses
        if method == "status":
            statuses += 1
            if statuses > 1:
                assert await get_client().xlen(service.events_key(conversation.id)) == 5
            return {
                "status": "running" if statuses == 1 else terminal_state,
                "messageId": message.id,
                "epoch": "sandbox-one",
                "sequence": 4,
                "archivePath": "/home/druks/work/chat/session.tar.gz",
            }
        assert method == "events"
        return {"events": [event for event in events if event["sequence"] > values["after"]]}

    async def download(**values):
        values["local"].write_bytes(b"archive")

    monkeypatch.setattr(Bridge, "request", request)
    host.download.side_effect = download

    await service.deliver_pending(druks_db, conversation)

    reply = (await list_messages(druks_db, conversation))[-1]
    assert message.state == terminal_state
    assert reply.role == MessageRole.ASSISTANT
    assert reply.reply_to == message.id
    assert not reply.state
    assert reply.body == "Found it."
    assert reply.tool_calls == [
        {
            "sessionUpdate": "tool_call_update",
            "toolCallId": "tool-1",
            "title": "Get ticket",
            "status": "completed",
            "rawInput": {"run": "run-one"},
            "rawOutput": {"gate": "approval"},
            "textOffset": 9,
        }
    ]
    assert conversation.title == "Gate check"
    assert await get_client().xlen(service.events_key(conversation.id)) == 2
    assert previous.deleted_at
    archive = await druks_db.get(FileRecord, conversation.session_file.id)
    assert not archive.agent_call_id
    assert not archive.uploaded_by
    assert archive.size == 7
    assert host.upload_file.await_count == 0


async def test_bridge_start_uploads_the_bridge_only_when_none_answers(monkeypatch):
    host = SimpleNamespace(
        ssh_username="druks",
        upload_file=AsyncMock(),
        exec=AsyncMock(return_value=SimpleNamespace(ok=True)),
    )
    answers = iter([False, True, True])
    monkeypatch.setattr(Bridge, "is_running", AsyncMock(side_effect=lambda: next(answers)))

    await Bridge(host).start()
    await Bridge(host).start()

    [upload] = host.upload_file.await_args_list
    assert upload.kwargs["local"].is_file()
    assert upload.kwargs["remote"].endswith("/druks-chat-bridge.mjs")
    host.exec.assert_awaited_once()


async def test_new_sandbox_restores_archive_and_drains_pending_messages(
    druks_db, conversation, sandbox, tmp_path, monkeypatch
):
    monkeypatch.setenv("DRUKS_DATA_DIR", str(tmp_path))
    previous = FileRecord(
        id="saved-session",
        name="session.tar.gz",
        size=1,
        content_type="application/gzip",
        sha256="saved",
        app="chat",
    )
    druks_db.add(previous)
    await druks_db.flush()
    conversation.session_file = File(id=previous.id)
    first = await conversation.get_unanswered_message(druks_db)
    second = await conversation.create_message(druks_db, "Continue")
    state = {"status": "missing", "sessionId": "", "messageId": ""}
    prompts = []
    starts = []

    async def request(self, method, **values):
        if method == "start":
            starts.append(values)
            state["sessionId"] = "restored"
        elif method == "prompt":
            prompts.append(values["messageId"])
            state.update(
                status="replied",
                messageId=values["messageId"],
                epoch="new-sandbox",
                sequence=0,
                archivePath="/home/druks/work/chat/session.tar.gz",
            )
        elif method == "events":
            return {"events": []}
        return state

    async def download(**values):
        values["local"].write_bytes(b"archive")

    host, _identity = sandbox
    host.download.side_effect = download
    monkeypatch.setattr(Bridge, "request", request)

    await service.deliver_pending(druks_db, conversation)

    assert prompts == [first.id, second.id]
    assert first.state == second.state == "replied"
    assert starts[0]["archivePath"].endswith("/restore.tar.gz")
    assert starts[1]["archivePath"] == ""
    assert host.upload_file.await_count == 1
    assert previous.deleted_at


async def test_each_open_page_receives_the_same_live_event(druks_db, conversation, tmp_path):
    api = configure_app_for_test(settings=make_settings(tmp_path))
    pages = []
    tasks = []
    for _ in range(2):
        ready = asyncio.Event()
        received = []

        async def send_json(event, *, ready=ready, received=received):
            received.append(event)
            ready.set()
            if event["type"] == "event":
                raise WebSocketDisconnect()

        socket = SimpleNamespace(app=api, send_json=send_json)
        tasks.append(
            asyncio.create_task(
                routes.stream_conversation(socket, conversation.id, conversation.account_id)
            )
        )
        await asyncio.wait_for(ready.wait(), 2)
        pages.append(received)
    event = {"type": "event", "messageId": 1, "sequence": 1, "epoch": "one"}
    await service.publish(conversation.id, event)
    await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 2)
    assert pages[0][-1] == pages[1][-1] == event


async def test_http_routes_are_private_and_reject_bearer_tokens(druks_db, conversation, tmp_path):
    api = configure_app_for_test(
        settings=make_settings(tmp_path, identity={"mode": "header", "header": "X-User"}),
        authenticated=False,
    )
    async with asgi_client(api) as client:
        headers = {"X-User": "other@example.com"}
        for method, suffix, body in [
            ("GET", "", None),
            ("POST", "/messages", {"body": "hello"}),
            ("POST", "/cancel", {"messageId": "missing"}),
        ]:
            response = await client.request(
                method,
                f"/api/chat/conversations/{conversation.id}{suffix}",
                headers=headers,
                json=body,
            )
            assert response.status_code == 404
        response = await client.get("/api/chat/conversations", headers=headers)
        assert response.json() == []
        response = await client.post(
            "/api/chat/conversations",
            json={"body": "hello"},
            headers={"Authorization": "Bearer invalid", "X-User": "owner@example.com"},
        )
        assert response.status_code == 401


async def test_first_message_creates_the_conversation_and_the_list_reads_counts_and_replies(
    druks_db, conversation, tmp_path
):
    api = configure_app_for_test(
        settings=make_settings(tmp_path, identity={"mode": "header", "header": "X-User"}),
        authenticated=False,
    )
    headers = {"X-User": "owner@example.com"}
    async with asgi_client(api) as client:
        response = await client.post(
            "/api/chat/conversations",
            json={"body": "  \n  Read   the gate\nThen report."},
            headers=headers,
        )
        assert response.status_code == 201
        created = response.json()
        assert not created["title"]
        assert created["messageCount"] == 1
        [first] = created["messages"]
        assert first["state"] == "pending"
        delivered = await druks_db.get(Message, first["id"])
        await delivered.mark_delivered(druks_db)
        await druks_db.commit()
        response = await client.post(
            f"/api/chat/conversations/{created['id']}/messages",
            json={"body": "Then read failures."},
            headers=headers,
        )
        assert response.status_code == 202
        response = await client.get("/api/chat/conversations", headers=headers)
        newest, _previous = response.json()
        assert newest["id"] == created["id"]
        assert newest["messageCount"] == 2
        assert newest["activeMessageId"] == first["id"]
        detail = await client.get(f"/api/chat/conversations/{created['id']}", headers=headers)
        assert detail.json()["activeMessageId"] == first["id"]
        assert len(detail.json()["messages"]) == 2
        response = await client.post(
            "/api/chat/conversations", json={"body": "   "}, headers=headers
        )
        assert response.status_code == 422


@pytest.mark.parametrize(
    "origin,username",
    [
        ("https://other.example.com", "owner@example.com"),
        ("http://testserver", "other@example.com"),
    ],
)
async def test_websocket_rejects_wrong_origin_or_owner(
    druks_db, conversation, tmp_path, origin, username
):
    api = configure_app_for_test(
        settings=make_settings(tmp_path, identity={"mode": "header", "header": "X-User"}),
        authenticated=False,
    )
    sent = []

    async def send(message):
        sent.append(message)

    websocket = WebSocket(
        {
            "type": "websocket",
            "app": api,
            "headers": [
                (b"host", b"testserver"),
                (b"origin", origin.encode()),
                (b"x-user", username.encode()),
            ],
        },
        receive=AsyncMock(),
        send=send,
    )
    await routes.conversation_socket(websocket, conversation.id)
    assert sent == [{"type": "websocket.close", "code": 1008, "reason": ""}]


@pytest.mark.parametrize("cancel_first", [True, False])
async def test_pending_send_and_cancel_have_one_winner_in_postgres(druks_db, cancel_first):
    engine = druks_db.bind.engine
    async with get_session(engine) as session:
        account = await Account.get_or_create(session, f"chat-race-{uuid4()}@example.com")
        conversation = await Conversation.create(session, account_id=account.id, body="Race")
        message = await conversation.get_unanswered_message(session)
        await session.commit()
    try:
        async with get_session(engine) as sender, get_session(engine) as stopper:
            send_message = await sender.get(Message, message.id)
            stop_message = await stopper.get(Message, message.id)
            if cancel_first:
                assert await stop_message.cancel_pending(stopper)
                other_update = asyncio.create_task(send_message.mark_delivered(sender))
                await stopper.commit()
                assert not await other_update
                await sender.commit()
            else:
                assert await send_message.mark_delivered(sender)
                other_update = asyncio.create_task(stop_message.cancel_pending(stopper))
                await sender.commit()
                assert not await other_update
                await stopper.commit()
        async with get_session(engine) as session:
            saved = await session.get(Message, message.id)
            assert saved.state == ("cancelled" if cancel_first else "delivered")
            assert bool(saved.delivered_at) != cancel_first
    finally:
        async with get_session(engine) as session:
            await session.execute(delete(Conversation).where(Conversation.id == conversation.id))
            await session.execute(delete(Account).where(Account.id == account.id))
            await session.commit()


async def test_stop_during_startup_keeps_the_sandbox_and_sends_only_the_next_message(
    druks_db, conversation, sandbox, monkeypatch
):
    host, identity = sandbox
    first = await conversation.get_unanswered_message(druks_db)
    second = await conversation.create_message(druks_db, "Next")
    startup = asyncio.Event()
    finish_startup = asyncio.Event()
    sandbox_requests = 0
    prompts = []

    async def get_sandbox(session, account_id, config):
        nonlocal sandbox_requests
        sandbox_requests += 1
        await session.commit()
        startup.set()
        await finish_startup.wait()
        return host, identity

    async def request(self, method, **values):
        if method == "prompt":
            prompts.append(values["messageId"])
        return {"status": "idle", "sessionId": "one"}

    async def follow_turn(session, conversation, message, bridge):
        message.state = MessageState.REPLIED
        await session.commit()

    monkeypatch.setattr(service, "get_sandbox", get_sandbox)
    monkeypatch.setattr(Bridge, "request", request)
    monkeypatch.setattr(service, "follow_turn", follow_turn)
    delivery = asyncio.create_task(service.deliver_pending(druks_db, conversation))
    try:
        await asyncio.wait_for(startup.wait(), 2)
        await service.cancel_turn(druks_db, conversation, first)
        assert first.state == "cancelled"
        assert not first.delivered_at
        assert sandbox_requests == 1
        assert not delivery.done()
        finish_startup.set()
        await asyncio.wait_for(delivery, 2)
    finally:
        finish_startup.set()
        await asyncio.gather(delivery, return_exceptions=True)
    assert sandbox_requests == 2
    assert prompts == [second.id]
    assert first.state == "cancelled"
    assert second.state == "replied"


async def test_stop_after_send_targets_the_message_and_leaves_the_queue(
    druks_db, conversation, sandbox, monkeypatch
):
    first = await conversation.get_unanswered_message(druks_db)
    assert await first.mark_delivered(druks_db)
    second = await conversation.create_message(druks_db, "Next")
    await druks_db.commit()
    request = AsyncMock(return_value={})
    monkeypatch.setattr(Bridge, "request", request)

    await service.cancel_turn(druks_db, conversation, first)

    request.assert_awaited_once_with("cancel", conversationId=conversation.id, messageId=first.id)
    assert second.state == "pending"


async def test_cancel_route_only_changes_the_requested_pending_message(
    druks_db, conversation, tmp_path, monkeypatch
):
    first = await conversation.get_unanswered_message(druks_db)
    second = await conversation.create_message(druks_db, "Queued")
    other = await Conversation.create(druks_db, account_id=conversation.account_id, body="Other")
    other_message = await other.get_unanswered_message(druks_db)
    await druks_db.commit()
    api = configure_app_for_test(
        settings=make_settings(tmp_path, identity={"mode": "header", "header": "X-User"}),
        authenticated=False,
    )
    get_running_sandbox = AsyncMock(
        side_effect=AssertionError("Pending cancellation must not reach the sandbox.")
    )
    monkeypatch.setattr(service, "get_running_sandbox", get_running_sandbox)
    headers = {"X-User": "owner@example.com"}
    endpoint = f"/api/chat/conversations/{conversation.id}/cancel"
    async with asgi_client(api) as client:
        response = await client.post(
            endpoint, json={"messageId": other_message.id}, headers=headers
        )
        assert response.status_code == 404
        response = await client.post(endpoint, json={"messageId": second.id}, headers=headers)
        assert response.status_code == 204
        response = await client.post(endpoint, json={"messageId": second.id}, headers=headers)
        assert response.status_code == 204
        detail = await client.get(f"/api/chat/conversations/{conversation.id}", headers=headers)
    assert [message["state"] for message in detail.json()["messages"]] == ["pending", "cancelled"]
    assert all(not message["deliveredAt"] for message in detail.json()["messages"])
    await druks_db.refresh(first)
    assert first.state == "pending"
    get_running_sandbox.assert_not_called()

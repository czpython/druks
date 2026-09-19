import asyncio
import importlib.util
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from druks.accounts.models import Account
from druks.chat import routes, sockets
from druks.chat.enums import MessageRole
from druks.chat.models import Conversation
from druks.models import Base
from druks.testing import asgi_client, configure_app_for_test, make_settings
from fastapi import WebSocketDisconnect
from sqlalchemy import text


async def test_conversation_recency_comes_from_messages_not_pins(druks_db):
    owner = await Account.get_or_create(druks_db, "pins@example.com")
    earlier = await Conversation.create(druks_db, account_id=owner.id, body="First")
    later = await Conversation.create(druks_db, account_id=owner.id, body="Second")
    empty = Conversation(account_id=owner.id, created_at=Base.utc_now() - timedelta(days=1))
    druks_db.add(empty)
    await druks_db.flush()
    reply = await earlier.create_message(druks_db, "Done", role=MessageRole.ASSISTANT)
    reply.created_at = later.created_at + timedelta(seconds=1)
    later.pinned = True
    await druks_db.flush()
    for conversation in (earlier, later, empty):
        await druks_db.refresh(conversation)

    conversations = await Conversation.list_for_account(druks_db, owner.id)

    assert [conversation.id for conversation in conversations] == [earlier.id, later.id, empty.id]
    assert earlier.last_message_at == earlier.last_reply_at == reply.created_at
    assert empty.last_message_at == empty.created_at
    assert not empty.last_reply_at
    assert not empty.pinned
    assert not later.last_reply_at

    reply.created_at = later.last_message_at
    await druks_db.flush()
    conversations = await Conversation.list_for_account(druks_db, owner.id)
    assert [conversation.id for conversation in conversations] == [later.id, earlier.id, empty.id]


async def test_owner_can_pin_and_unpin_without_changing_messages(druks_db, tmp_path, monkeypatch):
    owner = await Account.get_or_create(druks_db, "pins@example.com")
    conversation = await Conversation.create(druks_db, account_id=owner.id, body="Read the runs")
    await druks_db.commit()
    api = configure_app_for_test(
        settings=make_settings(tmp_path, identity={"mode": "header", "header": "X-User"}),
        authenticated=False,
    )
    publish = AsyncMock()
    monkeypatch.setattr(routes, "publish", publish)
    endpoint = f"/api/chat/conversations/{conversation.id}"
    headers = {"X-User": "pins@example.com"}
    async with asgi_client(api) as client:
        original = (await client.get(endpoint, headers=headers)).json()
        for pinned in (True, True, False):
            response = await client.patch(endpoint, json={"pinned": pinned}, headers=headers)
            assert response.status_code == 200
            assert response.json()["pinned"] == pinned
            detail = (await client.get(endpoint, headers=headers)).json()
            listed = (await client.get("/api/chat/conversations", headers=headers)).json()[0]
            assert detail["pinned"] == listed["pinned"] == pinned
            assert detail["messages"] == original["messages"]
            assert detail["lastMessageAt"] == original["lastMessageAt"]
            assert detail["lastReplyAt"] == original["lastReplyAt"]
            publish.assert_awaited_with(conversation.id, {"type": "messages"})


@pytest.mark.parametrize("body", [{}, {"pinned": "false"}, {"pinned": 1}, {"pinned": None}])
async def test_pin_requires_a_boolean(druks_db, tmp_path, body):
    owner = await Account.get_or_create(druks_db, "pins@example.com")
    conversation = await Conversation.create(druks_db, account_id=owner.id, body="Read the runs")
    api = configure_app_for_test(
        settings=make_settings(tmp_path, identity={"mode": "header", "header": "X-User"}),
        authenticated=False,
    )
    async with asgi_client(api) as client:
        response = await client.patch(
            f"/api/chat/conversations/{conversation.id}",
            json=body,
            headers={"X-User": "pins@example.com"},
        )
    assert response.status_code == 422


async def test_pin_rejects_foreign_and_missing_conversations_and_bearer_tokens(druks_db, tmp_path):
    owner = await Account.get_or_create(druks_db, "pins@example.com")
    conversation = await Conversation.create(druks_db, account_id=owner.id, body="Private")
    api = configure_app_for_test(
        settings=make_settings(tmp_path, identity={"mode": "header", "header": "X-User"}),
        authenticated=False,
    )
    async with asgi_client(api) as client:
        for conversation_id in (conversation.id, "missing"):
            response = await client.patch(
                f"/api/chat/conversations/{conversation_id}",
                json={"pinned": True},
                headers={"X-User": "other@example.com"},
            )
            assert response.status_code == 404
        response = await client.patch(
            f"/api/chat/conversations/{conversation.id}",
            json={"pinned": True},
            headers={"X-User": "pins@example.com", "Authorization": "Bearer invalid"},
        )
        assert response.status_code == 401
    await druks_db.refresh(conversation)
    assert not conversation.pinned


async def test_snapshot_contains_saved_pin_and_reply_times(druks_db, tmp_path):
    owner = await Account.get_or_create(druks_db, "pins@example.com")
    conversation = await Conversation.create(druks_db, account_id=owner.id, body="Read the runs")
    reply = await conversation.create_message(druks_db, "Done", role=MessageRole.ASSISTANT)
    conversation.pinned = True
    await druks_db.commit()
    events = []

    async def send_json(event):
        events.append(event)
        raise WebSocketDisconnect()

    socket = SimpleNamespace(
        app=configure_app_for_test(settings=make_settings(tmp_path)), send_json=send_json
    )
    with pytest.raises(WebSocketDisconnect):
        await asyncio.wait_for(
            sockets.stream_conversation(socket, conversation.id, owner.id), timeout=2
        )
    [snapshot] = events
    assert snapshot["pinned"]
    assert snapshot["lastMessageAt"] == snapshot["lastReplyAt"]
    assert datetime.fromisoformat(snapshot["lastReplyAt"]) == reply.created_at


async def test_pin_migration_defaults_existing_rows_and_reverses(druks_db):
    owner = await Account.get_or_create(druks_db, "pins@example.com")
    conversation = await Conversation.create(druks_db, account_id=owner.id, body="Keep this")
    spec = importlib.util.spec_from_file_location(
        "chat_pins",
        Path(__file__).parents[1] / "migrations/versions/32e4344e7ecf_pin_chat_conversations.py",
    )
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    def migrate(connection, direction):
        with Operations.context(MigrationContext.configure(connection)):
            getattr(migration, direction)()

    connection = await druks_db.connection()
    await connection.run_sync(migrate, "downgrade")
    await connection.run_sync(migrate, "upgrade")
    assert not await druks_db.scalar(
        text("SELECT pinned FROM chat_conversations WHERE id=:id"), {"id": conversation.id}
    )
    await connection.run_sync(migrate, "downgrade")
    assert (
        await druks_db.scalar(
            text("SELECT body FROM chat_messages WHERE conversation_id=:id"),
            {"id": conversation.id},
        )
        == "Keep this"
    )
    await connection.run_sync(migrate, "upgrade")

import inspect
from unittest.mock import AsyncMock

import pytest
from druks.accounts.models import Account
from druks.apps.registry import channels, services
from druks.chat import service
from druks.chat.enums import MessageRole, MessageState
from druks.chat.exceptions import ChatBridgeError
from druks.chat.models import Conversation, Message
from druks.database import session_scope
from druks.files.datastructures import File
from druks.files.models import FileRecord
from druks.secrets.datastructures import Audience
from druks.secrets.models import VaultSecret
from druks.testing import asgi_client, configure_app_for_test, make_settings
from sqlalchemy import select


@pytest.mark.parametrize("source,held", [("web", False), ("slack", False), ("slack", True)])
async def test_a_failed_delivery_ends_the_pending_messages_and_replies_on_a_live_channel(
    druks_db, monkeypatch, source, held
):
    account = await Account.get_or_create(druks_db, "op@example.com")
    conversation = await Conversation.create(druks_db, account_id=account.id, body="Read the run")
    delivered = await conversation.get_unanswered_message(druks_db)
    delivered.state = MessageState.DELIVERED
    pending = await conversation.create_message(druks_db, "And its logs")
    conversation.source = source
    send_reply = AsyncMock()
    if source != "web":
        connection = await VaultSecret.store(
            druks_db,
            services.get(source).secret_kind,
            Audience.service(source),
            identity={},
            secrets={},
        )
        conversation.connection_id = connection.id
        monkeypatch.setattr(channels.get(source), "send_reply", send_reply)
    await druks_db.commit()

    async def run_inline(options, function, *args):
        return await function(*args)

    monkeypatch.setattr(service, "step_session", session_scope)
    monkeypatch.setattr(service.DBOS, "run_step_async", run_inline)
    monkeypatch.setattr(
        service, "get_running_sandbox", AsyncMock(side_effect=ChatBridgeError("down"))
    )
    monkeypatch.setattr(Conversation, "is_held", AsyncMock(return_value=held))

    await inspect.unwrap(service.deliver)(conversation.id)

    await druks_db.refresh(delivered)
    await druks_db.refresh(pending)
    assert (delivered.state, pending.state) == (MessageState.DELIVERED, MessageState.FAILED)
    replies = list(
        await druks_db.scalars(select(Message).where(Message.role == MessageRole.ASSISTANT))
    )
    if source == "web" or held:
        assert replies == []
        send_reply.assert_not_awaited()
    else:
        [reply] = replies
        assert reply.reply_to == pending.id
        assert send_reply.await_args.args[2].id == reply.id


async def test_retry_keeps_the_attachment_and_the_failed_history(druks_db, monkeypatch, tmp_path):
    account = await Account.get_or_create(druks_db, "op@example.com")
    conversation = await Conversation.create(druks_db, account_id=account.id, body="")
    message = await conversation.get_unanswered_message(druks_db)
    file = FileRecord(
        app="chat",
        name="photo.png",
        content_type="image/png",
        size=5,
        sha256="a" * 64,
        uploaded_by=account.id,
    )
    druks_db.add(file)
    await druks_db.flush()
    message.file = File(id=file.id, name=file.name, content_type=file.content_type, size=file.size)
    message.state = MessageState.FAILED
    await druks_db.commit()
    started = AsyncMock()
    monkeypatch.setattr("druks.chat.routes.DBOS.start_workflow_async", started)
    app = configure_app_for_test(settings=make_settings(tmp_path))

    async with asgi_client(app) as client:
        response = await client.post(
            f"/api/chat/conversations/{conversation.id}/messages/{message.id}/retry"
        )

    assert response.status_code == 202
    assert response.json()["id"] != message.id
    assert response.json()["body"] == ""
    assert response.json()["state"] == "pending"
    assert response.json()["file"]["id"] == file.id
    await druks_db.refresh(message)
    assert message.state == MessageState.FAILED
    started.assert_awaited_once()


@pytest.mark.parametrize(
    "other_account,state,status",
    [(True, "failed", 404), (False, "replied", 409)],
)
async def test_retry_requires_ownership_and_a_retryable_message(
    druks_db, monkeypatch, tmp_path, other_account, state, status
):
    account = await Account.get_or_create(
        druks_db, "other@example.com" if other_account else "op@example.com"
    )
    conversation = await Conversation.create(druks_db, account_id=account.id, body="Read the run")
    message = await conversation.get_unanswered_message(druks_db)
    message.state = state
    await druks_db.commit()
    started = AsyncMock()
    monkeypatch.setattr("druks.chat.routes.DBOS.start_workflow_async", started)
    app = configure_app_for_test(settings=make_settings(tmp_path))

    async with asgi_client(app) as client:
        response = await client.post(
            f"/api/chat/conversations/{conversation.id}/messages/{message.id}/retry"
        )

    assert response.status_code == status
    started.assert_not_awaited()

from contextlib import suppress
from typing import Annotated

from dbos import DBOS
from fastapi import APIRouter, Body, Depends, HTTPException
from githubkit.exception import RequestFailed
from slack_sdk.errors import SlackApiError

from druks.accounts.context import current_conversation_id
from druks.accounts.dependencies import current_account, current_session_account
from druks.accounts.models import Account
from druks.api.dependencies import SessionDep
from druks.apps.registry import channels

from .enums import MessageState
from .exceptions import ChannelHasNoThreadsError, ChatSandboxGone
from .models import Conversation, Message
from .schemas import ConversationDetailResponse, ConversationResponse, MessageResponse
from .service import cancel_turn, deliver, publish

router = APIRouter()


@router.get(
    "/conversations", response_model=list[ConversationResponse], response_model_by_alias=True
)
async def list_conversations(
    session: SessionDep, account: Account = Depends(current_session_account)
) -> list[Conversation]:
    return await Conversation.list_for_account(session, account.id)


@router.post(
    "/conversations",
    status_code=201,
    response_model=ConversationDetailResponse,
    response_model_by_alias=True,
)
async def create_conversation(
    session: SessionDep,
    body: Annotated[str, Body(embed=True)],
    account: Account = Depends(current_session_account),
) -> Conversation:
    if not body.strip():
        raise HTTPException(422, "Write a message.")
    conversation = await Conversation.create(session, account_id=account.id, body=body)
    await session.commit()
    await DBOS.start_workflow_async(deliver, conversation.id)
    await session.refresh(
        conversation,
        ["messages", "message_count", "active_message_id", "last_message_at"],
    )
    return conversation


@router.get(
    "/conversations/{conversation_id}",
    response_model=ConversationDetailResponse,
    response_model_by_alias=True,
)
async def get_conversation(
    conversation_id: str, session: SessionDep, account: Account = Depends(current_session_account)
) -> Conversation:
    conversation = await Conversation.get_for_account(session, conversation_id, account.id)
    if not conversation:
        raise HTTPException(404, "Conversation not found.")
    await session.refresh(conversation, ["messages"])
    return conversation


@router.patch(
    "/conversations/{conversation_id}",
    response_model=ConversationResponse,
    response_model_by_alias=True,
)
async def set_conversation_pinned(
    conversation_id: str,
    session: SessionDep,
    is_pinned: Annotated[bool, Body(embed=True)],
    account: Account = Depends(current_session_account),
) -> Conversation:
    conversation = await Conversation.get_for_account(session, conversation_id, account.id)
    if not conversation:
        raise HTTPException(404, "Conversation not found.")
    conversation.is_pinned = is_pinned
    await session.commit()
    await session.refresh(conversation)
    return conversation


@router.post(
    "/conversations/{conversation_id}/messages",
    status_code=202,
    response_model=MessageResponse,
    response_model_by_alias=True,
)
async def create_message(
    conversation_id: str,
    session: SessionDep,
    body: Annotated[str, Body(embed=True)],
    account: Account = Depends(current_session_account),
) -> Message:
    conversation = await Conversation.get_for_account(session, conversation_id, account.id)
    if not conversation:
        raise HTTPException(404, "Conversation not found.")
    if not body.strip():
        raise HTTPException(422, "Write a message.")
    message = await conversation.create_message(session, body)
    await session.commit()
    await publish(conversation.id, {"type": "messages"})
    await DBOS.start_workflow_async(deliver, conversation.id)
    return message


@router.post(
    "/conversations/{conversation_id}/messages/{message_id}/retry",
    status_code=202,
    response_model=MessageResponse,
    response_model_by_alias=True,
)
async def retry_message(
    conversation_id: str,
    message_id: str,
    session: SessionDep,
    account: Account = Depends(current_session_account),
) -> Message:
    conversation = await Conversation.get_for_account(session, conversation_id, account.id)
    if not conversation:
        raise HTTPException(404, "Conversation not found.")
    original_message = await conversation.get_message(session, message_id)
    if not original_message:
        raise HTTPException(404, "Message not found.")
    if original_message.state not in (
        MessageState.FAILED,
        MessageState.INTERRUPTED,
        MessageState.CANCELLED,
    ):
        raise HTTPException(
            409,
            f"The message is {original_message.state}. Send again only a failed, "
            "interrupted, or cancelled message.",
        )
    message = await conversation.create_message(
        session,
        original_message.body,
        file=original_message.file,
        is_internal=original_message.is_internal,
    )
    await session.commit()
    await publish(conversation.id, {"type": "messages"})
    await DBOS.start_workflow_async(deliver, conversation.id)
    return message


@router.post("/conversations/{conversation_id}/cancel", status_code=204)
async def cancel(
    conversation_id: str,
    session: SessionDep,
    message_id: Annotated[str, Body(embed=True, alias="messageId")],
    account: Account = Depends(current_session_account),
) -> None:
    conversation = await Conversation.get_for_account(session, conversation_id, account.id)
    if not conversation:
        raise HTTPException(404, "Conversation not found.")
    message = await conversation.get_message(session, message_id)
    if not message:
        raise HTTPException(404, "Message not found.")
    with suppress(ChatSandboxGone):
        await cancel_turn(session, conversation, message)
    await DBOS.start_workflow_async(deliver, conversation.id)


@router.get("/thread", operation_id="read_thread", tags=["agent"])
async def read_thread(
    session: SessionDep, account: Account = Depends(current_account)
) -> list[dict]:
    """Read the thread of the conversation this call comes from: its newest 200
    messages, oldest first. Each one has ts, user_id, user_name, text, is_from_you (you
    wrote it), and is_from_user (the person you answer wrote it). A direct message has
    no thread."""
    conversation_id = current_conversation_id.get()
    if not conversation_id:
        raise HTTPException(
            409, "This tool reads a channel conversation's thread. Call it from one."
        )
    conversation = await session.get(Conversation, conversation_id)
    try:
        return await channels.get(conversation.source).read_thread(session, conversation)
    except ChannelHasNoThreadsError as error:
        raise HTTPException(409, str(error)) from error
    except (SlackApiError, RequestFailed) as error:
        raise HTTPException(502, str(error)) from error


@router.put("/thread/tag", operation_id="require_tag", status_code=204, tags=["agent"])
async def require_tag(
    session: SessionDep,
    is_required: Annotated[bool, Body(embed=True)],
    account: Account = Depends(current_account),
) -> None:
    """Require a tag in the thread of the conversation this call comes from, or stop
    requiring one. While a tag is required, the person's untagged replies in the thread
    do not reach you. Their tagged messages always do."""
    conversation_id = current_conversation_id.get()
    if not conversation_id:
        raise HTTPException(
            409, "This tool sets a channel conversation's thread. Call it from one."
        )
    conversation = await session.get(Conversation, conversation_id)
    if not conversation.thread_id:
        raise HTTPException(409, "A direct message has no thread. Every message reaches you.")
    conversation.is_tag_required = is_required
    await session.commit()

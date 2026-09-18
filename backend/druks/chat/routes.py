from contextlib import suppress
from typing import Annotated

from dbos import DBOS
from fastapi import APIRouter, Body, Depends, HTTPException

from druks.accounts.dependencies import current_session_account
from druks.accounts.models import Account
from druks.api.dependencies import SessionDep

from .exceptions import ChatSandboxGone
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
    await session.refresh(conversation, ["messages", "message_count", "active_message_id"])
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

import asyncio
import json
from contextlib import suppress
from typing import Annotated

from dbos import DBOS
from fastapi import APIRouter, Body, Depends, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.encoders import jsonable_encoder

from druks.accounts.dependencies import current_session_account, require_operator
from druks.accounts.models import Account
from druks.api.dependencies import SessionDep
from druks.browser.login import is_same_origin
from druks.database import get_session
from druks.redis import get_client

from .exceptions import ChatSandboxGone
from .models import Conversation, Message
from .schemas import ConversationDetailResponse, ConversationResponse, MessageResponse
from .service import cancel_turn, deliver, events_key, publish

router = APIRouter(prefix="/api/chat", tags=["chat"])


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


@router.websocket("/conversations/{conversation_id}/ws")
async def conversation_socket(websocket: WebSocket, conversation_id: str) -> None:
    if not is_same_origin(websocket):
        await websocket.close(code=1008)
        return
    engine = websocket.app.state.engine
    async with get_session(engine) as session:
        try:
            account = await require_operator(session, websocket)
        except HTTPException:
            await websocket.close(code=1008)
            return
        conversation = await Conversation.get_for_account(session, conversation_id, account.id)
        if not conversation:
            await websocket.close(code=1008)
            return
        await session.commit()
    await websocket.accept()
    await DBOS.start_workflow_async(deliver, conversation_id)
    try:
        async with asyncio.TaskGroup() as group:
            streaming = group.create_task(
                stream_conversation(websocket, conversation_id, account.id)
            )
            # The page never sends: receive() returns when it leaves.
            await websocket.receive()
            streaming.cancel()
    except* WebSocketDisconnect:
        pass


async def stream_conversation(websocket: WebSocket, conversation_id: str, account_id: str) -> None:
    position = "0-0"
    events = [{"type": "messages"}]
    while True:
        for event in events:
            if event["type"] == "messages":
                async with get_session(websocket.app.state.engine) as session:
                    conversation = await Conversation.get_for_account(
                        session, conversation_id, account_id
                    )
                    if not conversation:
                        await websocket.close(code=1008)
                        return
                    await session.refresh(conversation, ["messages"])
                    snapshot = ConversationDetailResponse.model_validate(conversation)
                    await session.commit()
                await websocket.send_json({"type": "snapshot", **jsonable_encoder(snapshot)})
            else:
                await websocket.send_json(event)
        batches = await get_client().xread(
            {events_key(conversation_id): position}, count=128, block=1000
        )
        events = []
        for _key, entries in batches:
            for cursor, fields in entries:
                events.append(json.loads(fields[b"event"]))
                position = cursor

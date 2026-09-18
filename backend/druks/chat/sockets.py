import asyncio
import json

from dbos import DBOS
from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.encoders import jsonable_encoder

from druks.accounts.dependencies import require_operator
from druks.browser.login import is_same_origin
from druks.database import get_session
from druks.redis import get_client

from .models import Conversation
from .schemas import ConversationDetailResponse
from .service import deliver, events_key

# The server mounts this router itself: the app loader's identity gate reads an HTTP
# request, and a WebSocket upgrade has none.
router = APIRouter(prefix="/api/chat", tags=["chat"])


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

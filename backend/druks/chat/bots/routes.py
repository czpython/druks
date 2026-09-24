from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path

from druks.accounts.dependencies import current_account, current_session_account
from druks.accounts.models import Account
from druks.api.dependencies import SessionDep
from druks.apps.loader import get_app
from druks.chat.enums import BotAccess
from druks.chat.models import Conversation

from .constants import ADMIN_CODE_TTL_SECONDS
from .schemas import AdminCodeResponse, ResumeConversationResponse
from .service import create_admin_code, get_bot_connection, resume

router = APIRouter()


@router.post(
    "/conversations/{conversation_id}/resume",
    operation_id="resume_conversation",
    tags=["bot"],
    openapi_extra={"x-destructive": False, "x-idempotent": True},
    response_model=ResumeConversationResponse,
    response_model_by_alias=True,
)
async def resume_conversation(
    session: SessionDep,
    conversation_id: Annotated[
        str, Path(description="The paused conversation, from Druks's message.")
    ],
    account: Account = Depends(current_account),
) -> ResumeConversationResponse:
    """Let the bot answer a chat again, before the pause ends by itself, after a person
    answered it from the connection's phone. Only the connection's admin can resume its
    chats."""
    conversation = await session.get(Conversation, conversation_id)
    if conversation and conversation.admin_account_id == account.id:
        result = "resumed" if await resume(session, conversation) else "not_paused"
        return ResumeConversationResponse(conversation=conversation.id, result=result)
    raise HTTPException(404, "Conversation not found.")


@router.post(
    "/connections/{connection_id}/admin-code",
    response_model=AdminCodeResponse,
    response_model_by_alias=True,
)
async def add_admin_code(
    session: SessionDep,
    connection_id: str,
    account: Account = Depends(current_session_account),
) -> AdminCodeResponse:
    """A one-time code to connect a phone to the connection."""
    connection = await get_bot_connection(session, connection_id)
    if connection:
        code = await create_admin_code(connection, account.id)
        return AdminCodeResponse(code=code, expires_in=ADMIN_CODE_TTL_SECONDS)
    raise HTTPException(404, "Connection not found.")


@router.delete("/connections/{connection_id}/pairing", status_code=204)
async def unpair(
    session: SessionDep,
    connection_id: str,
    account: Account = Depends(current_session_account),
) -> None:
    """Remove the signed-in operator's phones from this connection."""
    connection = await get_bot_connection(session, connection_id)
    if connection and get_app(connection.identity["app"]).bot.access == BotAccess.PAIRED:
        connection.identity = {
            **connection.identity,
            "operators": {
                sender: operator
                for sender, operator in connection.identity["operators"].items()
                if operator != account.id
            },
        }
        return
    raise HTTPException(404, "Connection not found.")

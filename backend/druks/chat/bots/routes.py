from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path

from druks.accounts.dependencies import current_account, current_session_account
from druks.accounts.models import Account
from druks.api.dependencies import SessionDep
from druks.apps.loader import get_app
from druks.chat.enums import BotAccess
from druks.chat.models import Conversation

from .constants import ONE_TIME_CODE_TTL_SECONDS
from .schemas import OneTimeCodeResponse, ResumeConversationResponse
from .service import create_one_time_code, get_bot_connection, resume

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
    "/connections/{connection_id}/one-time-code",
    response_model=OneTimeCodeResponse,
    response_model_by_alias=True,
)
async def open_one_time_code(
    session: SessionDep,
    connection_id: str,
    account: Account = Depends(current_session_account),
) -> OneTimeCodeResponse:
    """A one-time code that pairs a phone, or makes its sender the number's admin."""
    connection = await get_bot_connection(session, connection_id)
    if connection:
        code = await create_one_time_code(connection, account.id)
        return OneTimeCodeResponse(code=code, expires_in=ONE_TIME_CODE_TTL_SECONDS)
    raise HTTPException(404, "Connection not found.")


@router.delete("/connections/{connection_id}/pairing", status_code=204)
async def unpair(
    session: SessionDep,
    connection_id: str,
    account: Account = Depends(current_session_account),
) -> None:
    """Remove the signed-in operator's phones from this connection."""
    connection = await get_bot_connection(session, connection_id)
    if connection and get_app(connection.identity["app"]).bot.access == BotAccess.PRIVATE:
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

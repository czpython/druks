from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path

from druks.accounts.dependencies import current_account, current_session_account
from druks.accounts.enums import AccountKind
from druks.accounts.models import Account
from druks.api.dependencies import SessionDep
from druks.chat.models import Conversation
from druks.secrets.enums import SecretKind
from druks.secrets.models import VaultSecret

from .constants import ADMIN_CODE_TTL_SECONDS
from .schemas import AdminCodeResponse, ResumeConversationResponse
from .service import open_admin_code, resume

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
    answered it from the number's phone. Only the number's admin can resume its chats."""
    conversation = await session.get(Conversation, conversation_id)
    if conversation and conversation.admin_account_id == account.id:
        result = "resumed" if await resume(session, conversation) else "not_paused"
        return ResumeConversationResponse(conversation=conversation.id, result=result)
    raise HTTPException(404, "Conversation not found.")


@router.post(
    "/connections/{connection_id}/admin-code",
    dependencies=[Depends(current_session_account)],
    response_model=AdminCodeResponse,
    response_model_by_alias=True,
)
async def add_admin_code(session: SessionDep, connection_id: str) -> AdminCodeResponse:
    """A one-time code. The person who sends it to a bot's number gets the number's
    questions on their own phone."""
    connection = await session.get(VaultSecret, connection_id)
    if (
        connection
        and connection.kind == SecretKind.SESSION
        and connection.is_live
        and connection.account.kind == AccountKind.BOT
    ):
        code = await open_admin_code(connection)
        return AdminCodeResponse(code=code, expires_in=ADMIN_CODE_TTL_SECONDS)
    raise HTTPException(404, "Number not found.")

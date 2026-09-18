from typing import Annotated

from fastapi import APIRouter, Body, Depends, HTTPException

from druks.accounts.dependencies import current_session_account
from druks.accounts.enums import AccountKind
from druks.accounts.models import Account
from druks.api.dependencies import SessionDep
from druks.apps.loader import get_app
from druks.chat.bots.service import resume
from druks.chat.models import Conversation
from druks.secrets.models import VaultSecret

from .schemas import QrResponse, SessionResponse
from .services import Waha

router = APIRouter(prefix="/services/waha")


@router.get("/sessions", response_model=list[SessionResponse], response_model_by_alias=True)
async def list_sessions(
    session: SessionDep, app: str = "", account: Account = Depends(current_session_account)
) -> list[VaultSecret]:
    """An app's numbers, or the operator's own number."""
    return await Waha.list_sessions(session, app=app, account_id=account.id)


@router.post(
    "/sessions", status_code=201, response_model=SessionResponse, response_model_by_alias=True
)
async def link_session(
    session: SessionDep,
    app: Annotated[str, Body(embed=True)] = "",
    account: Account = Depends(current_session_account),
) -> VaultSecret:
    """Link a number for an app's Bot, under new bot and admin accounts, or for the
    operator."""
    owner = account
    identity = {}
    if app:
        try:
            bot = get_app(app).bot
        except KeyError as exc:
            raise HTTPException(404, f"Unknown app {app!r}") from exc
        if not bot:
            raise HTTPException(404, f"App {app!r} declares no Bot.")
        owner = await Account.create_for_bot(session, AccountKind.BOT)
        admin = await Account.create_for_bot(session, AccountKind.BOT_ADMIN)
        identity = {"app": app, "admin": {"account_id": admin.id}}
    return await Waha.link(session, owner, identity=identity)


@router.get("/sessions/{session_id}/qr", response_model=QrResponse, response_model_by_alias=True)
async def get_qr(
    session: SessionDep, session_id: str, account: Account = Depends(current_session_account)
) -> dict[str, str]:
    connection = await Waha.get_session(session, session_id, account.id)
    if connection and connection.is_live:
        client = await Waha.get_client(session, connection)
        return await client.get_qr()
    raise HTTPException(404, "Session not found.")


@router.delete("/sessions/{session_id}", status_code=204)
async def remove_session(
    session: SessionDep, session_id: str, account: Account = Depends(current_session_account)
) -> None:
    connection = await Waha.get_session(session, session_id, account.id)
    if not connection:
        raise HTTPException(404, "Session not found.")
    # Removing is idempotent: a second delete finds the session removed.
    if connection.is_live:
        await Waha.unlink(session, connection, "user")
        # The number's chats never take a turn again, so their pauses end.
        for conversation in await Conversation.list_for_connection(session, connection.id):
            await resume(session, conversation)

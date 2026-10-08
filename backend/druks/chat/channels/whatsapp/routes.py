from typing import Annotated

from fastapi import APIRouter, Body, Depends, HTTPException

from druks.accounts.dependencies import current_session_account
from druks.accounts.enums import AccountKind
from druks.accounts.models import Account
from druks.api.dependencies import SessionDep
from druks.apps.loader import get_app
from druks.chat.enums import BotAccess
from druks.secrets.models import VaultSecret

from .schemas import QrResponse, SessionResponse
from .services import Waha

router = APIRouter(prefix="/services/waha", dependencies=[Depends(current_session_account)])


@router.get("/sessions", response_model=list[SessionResponse], response_model_by_alias=True)
async def list_sessions(
    session: SessionDep, app: str, account: Account = Depends(current_session_account)
) -> list[SessionResponse]:
    """The numbers of an app."""
    numbers = []
    for connection in await Waha.list_sessions(session, app=app):
        number = SessionResponse.model_validate(connection)
        if (
            connection.account.kind == AccountKind.BOT
            and get_app(connection.identity["app"]).bot.access == BotAccess.PAIRED
        ):
            number.is_phone_connected = account.id in connection.identity["operators"].values()
        numbers.append(number)
    return numbers


@router.post(
    "/sessions", status_code=201, response_model=SessionResponse, response_model_by_alias=True
)
async def link_session(session: SessionDep, app: Annotated[str, Body(embed=True)]) -> VaultSecret:
    """Link a number for the Bot of an app."""
    try:
        bot = get_app(app).bot
    except KeyError as exc:
        raise HTTPException(404, f"Unknown app {app!r}") from exc
    if not bot:
        raise HTTPException(404, f"App {app!r} declares no Bot.")
    owner = await Account.create_for_bot(session, AccountKind.BOT)
    if bot.access == BotAccess.PAIRED:
        identity = {"app": app, "operators": {}}
    else:
        admin = await Account.create_for_bot(session, AccountKind.BOT_ADMIN)
        identity = {"app": app, "admin": {"account_id": admin.id}}
    return await Waha.link(session, owner, identity=identity)


@router.get("/sessions/{session_id}/qr", response_model=QrResponse, response_model_by_alias=True)
async def get_qr(session: SessionDep, session_id: str) -> dict[str, str]:
    connection = await Waha.get_session(session, session_id)
    if connection and connection.is_live:
        client = await Waha.get_client(session, connection)
        return await client.get_qr()
    raise HTTPException(404, "Session not found.")


@router.post(
    "/sessions/{session_id}/relink", response_model=SessionResponse, response_model_by_alias=True
)
async def relink_session(session: SessionDep, session_id: str) -> VaultSecret:
    """Take a new scan on a number that lost its link."""
    connection = await Waha.get_session(session, session_id)
    if connection and connection.is_live:
        await Waha.relink(session, connection)
        return connection
    raise HTTPException(404, "Session not found.")


@router.delete("/sessions/{session_id}", status_code=204)
async def remove_session(session: SessionDep, session_id: str) -> None:
    connection = await Waha.get_session(session, session_id)
    if not connection:
        raise HTTPException(404, "Session not found.")
    # Removing is idempotent: a second delete finds the session removed.
    if connection.is_live:
        await Waha.unlink(session, connection, "user")

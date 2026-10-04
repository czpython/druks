from typing import Annotated

from fastapi import APIRouter, Body, Depends, HTTPException
from sqlalchemy import Row

from druks.accounts.dependencies import current_session_account
from druks.accounts.enums import AccountKind
from druks.accounts.models import Account
from druks.api.dependencies import SessionDep
from druks.apps.loader import get_app
from druks.chat.enums import BotAccess, ConversationSource
from druks.chat.models import Conversation
from druks.core.services import Twilio
from druks.secrets.models import VaultSecret
from druks.settings import load_settings

from .channel import CallsChannel
from .exceptions import CallsLinkError
from .schemas import CallResponse, LineResponse, LinkedNumberResponse, UnlinkedNumberResponse

router = APIRouter(prefix="/services/twilio", dependencies=[Depends(current_session_account)])


@router.get("/numbers", response_model=list[LinkedNumberResponse], response_model_by_alias=True)
async def list_numbers(session: SessionDep, app: str) -> list[VaultSecret]:
    """An app's phone numbers, removed ones included."""
    return await Twilio.list_connections(session, app)


@router.get(
    "/numbers/unlinked", response_model=list[UnlinkedNumberResponse], response_model_by_alias=True
)
async def list_unlinked_numbers(session: SessionDep) -> list[dict]:
    """The Twilio account's numbers that are free to link."""
    return await Twilio.list_unlinked_numbers(session)


@router.post(
    "/numbers", status_code=201, response_model=LinkedNumberResponse, response_model_by_alias=True
)
async def link_number(
    session: SessionDep,
    app: Annotated[str, Body(embed=True)],
    sid: Annotated[str, Body(embed=True)],
) -> VaultSecret:
    """Link a Twilio number to an app's open Bot. The number gets its own bot account."""
    try:
        bot = get_app(app).bot
    except KeyError as error:
        raise HTTPException(404, f"Unknown app {app!r}") from error
    if not bot or bot.access != BotAccess.OPEN:
        raise HTTPException(404, f"App {app!r} declares no open Bot.")
    webhook_base = load_settings().urls.webhook_base
    if not webhook_base:
        raise CallsLinkError(
            "Druks has no public address that Twilio can send calls to. "
            "Set urls.webhook_host or urls.endpoint."
        )
    if sid in await Twilio.list_linked_sids(session):
        raise CallsLinkError("This number is already linked. Remove it first.")
    owner = await Account.create_for_bot(session, AccountKind.BOT)
    calls_url = f"{webhook_base}/_external/twilio/calls/"
    return await Twilio.link(session, owner, app=app, sid=sid, calls_url=calls_url)


@router.delete("/numbers/{number_id}", status_code=204)
async def remove_number(session: SessionDep, number_id: str) -> None:
    if connection := await Twilio.get_connection(session, number_id):
        # Removing is idempotent: a second delete finds the number removed.
        if connection.is_live:
            await Twilio.unlink(session, connection, "user")
        return
    raise HTTPException(404, "Number not found.")


@router.get(
    "/numbers/{number_id}/calls", response_model=list[CallResponse], response_model_by_alias=True
)
async def list_calls(session: SessionDep, number_id: str) -> list[Row]:
    """A number's calls. The number's bot account owns them, and any operator reads them,
    as any operator manages an app's numbers."""
    if await Twilio.get_connection(session, number_id):
        return await CallsChannel.list_calls(session, number_id)
    raise HTTPException(404, "Number not found.")


@router.get(
    "/numbers/{number_id}/calls/{call_id}",
    response_model=list[LineResponse],
    response_model_by_alias=True,
)
async def list_lines(session: SessionDep, number_id: str, call_id: str) -> list[Row]:
    """A call's transcript: what the caller and the assistant said, in order."""
    call = await session.get(Conversation, call_id)
    if call and call.source == ConversationSource.CALL and call.connection_id == number_id:
        return await CallsChannel.list_lines(session, call_id)
    raise HTTPException(404, "Call not found.")

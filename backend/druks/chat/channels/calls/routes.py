from typing import Annotated

from fastapi import APIRouter, Body, Depends, HTTPException

from druks.accounts.dependencies import current_session_account
from druks.accounts.enums import AccountKind
from druks.accounts.models import Account
from druks.api.dependencies import SessionDep
from druks.apps.loader import get_app
from druks.chat.enums import BotAccess
from druks.secrets.models import VaultSecret

from .schemas import NumberResponse, TwilioNumberResponse
from .services import Twilio

router = APIRouter(prefix="/services/calls", dependencies=[Depends(current_session_account)])


@router.get("/numbers", response_model=list[NumberResponse], response_model_by_alias=True)
async def list_numbers(session: SessionDep, app: str) -> list[VaultSecret]:
    """An app's phone numbers, removed ones included."""
    return await Twilio.list_connections(session, app)


@router.get(
    "/twilio-numbers", response_model=list[TwilioNumberResponse], response_model_by_alias=True
)
async def list_twilio_numbers(session: SessionDep) -> list[dict]:
    """The Twilio account's numbers that are free to link."""
    return await Twilio.list_unlinked_numbers(session)


@router.post(
    "/numbers", status_code=201, response_model=NumberResponse, response_model_by_alias=True
)
async def link_number(
    session: SessionDep,
    app: Annotated[str, Body(embed=True)],
    sid: Annotated[str, Body(embed=True)],
) -> VaultSecret:
    """Link a Twilio number to an app's open Bot. The number gets its own bot account."""
    try:
        bot = get_app(app).bot
    except KeyError as exc:
        raise HTTPException(404, f"Unknown app {app!r}") from exc
    if bot and bot.access == BotAccess.OPEN:
        owner = await Account.create_for_bot(session, AccountKind.BOT)
        return await Twilio.link(session, owner, app=app, sid=sid)
    raise HTTPException(404, f"App {app!r} declares no open Bot.")


@router.delete("/numbers/{number_id}", status_code=204)
async def remove_number(session: SessionDep, number_id: str) -> None:
    connection = await Twilio.get_connection(session, number_id)
    if connection and connection.is_live:
        await Twilio.unlink(session, connection)
        return
    raise HTTPException(404, "Number not found.")

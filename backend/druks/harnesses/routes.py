import logging
from contextlib import suppress

from fastapi import APIRouter, Body, Depends, HTTPException

from druks.accounts.dependencies import current_session_account, current_session_or_setup
from druks.accounts.models import Account
from druks.accounts.schemas import AccountResponse
from druks.database import db_session
from druks.harnesses.base import Harness
from druks.harnesses.exceptions import ConnectError
from druks.harnesses.models import HarnessConnection
from druks.harnesses.registry import get_harness
from druks.user_settings.models import HarnessSettings, UserSettings
from druks.user_settings.schemas import HarnessResponse

router = APIRouter(prefix="/api/harnesses", tags=["harnesses"])


def _resolve_harness(name: str) -> type[Harness]:
    harness = get_harness(name)
    if harness:
        return harness
    raise HTTPException(status_code=404, detail=f"Unknown harness: {name!r}")


@router.post("/{name}/connection/start")
async def start_connection(
    name: str, account: Account | None = Depends(current_session_or_setup)
) -> dict[str, str]:
    harness = _resolve_harness(name)
    # A resolved operator binds the flow; none/zero starts the unbound setup
    # flow whose completion creates the operator.
    url, connection_id = await harness.connect_start(account_id=account.id if account else None)
    return {"authorizeUrl": url, "connectionId": connection_id}


@router.post(
    "/{name}/connection/complete",
    response_model=AccountResponse,
    response_model_by_alias=True,
)
async def complete_connection(
    name: str,
    account: Account | None = Depends(current_session_or_setup),
    code: str = Body(..., embed=True),
    connection_id: str = Body(..., embed=True, alias="connectionId"),
) -> AccountResponse:
    harness = _resolve_harness(name)
    try:
        completed = await harness.connect_complete(flow_id=connection_id, pasted=code)
    except ConnectError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if completed.account_id:
        # A bound flow must complete under the operator that started it —
        # never rebound by email fallback.
        if not account or account.id != completed.account_id:
            raise HTTPException(
                status_code=422,
                detail="This connect was started under a different operator — start it again.",
            )
        resolved = account
    else:
        # The unbound setup flow attaches to the operator this request resolved
        # — a flow started before the account existed still lands on it. Only a
        # still-account-less request creates the operator from the
        # provider-verified email; get_or_create is atomic, so concurrent
        # completions of the same email converge, and a true different-email
        # race surfaces as the none-mode multi-operator refusal.
        resolved = account or Account.get_or_create(completed.provider_email)
    # Runs with no actor execute as the fallback account; claim the slot when
    # none is set yet.
    settings = UserSettings.get()
    if not settings.fallback_account_id:
        settings.set_fallback_account(resolved.id)
    connection = HarnessConnection.connect(
        harness=harness.name,
        account=resolved,
        payload=completed.payload,
        expires_at=completed.expires_at,
        provider_email=completed.provider_email,
    )
    # Materialize the reply, then land the credential before any provider I/O —
    # an await while flushed rows still hold their locks can stall every other
    # writer on this event loop, and nothing past the point of durability may
    # depend on another database read.
    response = AccountResponse.model_validate(resolved)
    db_session().commit()
    try:
        # Fresh picker right after connect; fetch failures are tagged inside.
        # The single-use flow is already spent, so trouble here — including a
        # database that vanished under the refresh — only logs.
        await HarnessSettings.require(harness.name).refresh_models(connection)
    except Exception:
        logging.getLogger(__name__).exception("Model refresh after connect failed")
        with suppress(Exception):
            db_session().rollback()
    return response


@router.delete("/{name}/connection", response_model=HarnessResponse, response_model_by_alias=True)
async def disconnect_harness(
    name: str, account: Account = Depends(current_session_account)
) -> HarnessResponse:
    harness = _resolve_harness(name)
    connection = HarnessConnection.get_for_account(harness.name, account.id)
    if connection:
        # Only the requesting account's own connection — never another's.
        connection.delete()
    return HarnessResponse.from_row(HarnessSettings.require(harness.name), None, account)

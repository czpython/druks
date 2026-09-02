import logging
from contextlib import suppress

from fastapi import APIRouter, Body, Depends, HTTPException

from druks.accounts.dependencies import current_session_account, current_session_or_setup
from druks.accounts.models import Account
from druks.accounts.schemas import AccountResponse
from druks.database import db_session
from druks.user_settings.models import HarnessSettings, UserSettings

from .exceptions import ConnectError
from .models import ProviderLogin
from .providers import Provider, get_provider, get_providers
from .registry import get_harnesses
from .schemas import ProviderLoginResponse, ProviderResponse

router = APIRouter(prefix="/api/providers", tags=["providers"])


@router.get(
    "",
    response_model=list[ProviderResponse],
    response_model_by_alias=True,
    dependencies=[Depends(current_session_or_setup)],
)
async def list_providers() -> list[ProviderResponse]:
    return [ProviderResponse.model_validate(provider) for provider in get_providers()]


@router.get("/logins", response_model=list[ProviderLoginResponse], response_model_by_alias=True)
async def list_logins(
    account: Account = Depends(current_session_account),
) -> list[ProviderLoginResponse]:
    logins = await ProviderLogin.list_for_account(account.id)
    return [ProviderLoginResponse.model_validate(login) for login in logins]


def _resolve_provider(provider_id: str) -> Provider:
    try:
        return get_provider(provider_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail=f"Unknown provider: {provider_id!r}") from error


@router.post("/{provider_id}/connection/start")
async def start_connection(
    provider_id: str, account: Account | None = Depends(current_session_or_setup)
) -> dict[str, str]:
    provider = _resolve_provider(provider_id)
    # A resolved operator binds the flow; none/zero starts the unbound setup
    # flow whose completion creates the operator.
    url, flow_id = await provider.connect_start(account_id=account.id if account else None)
    return {"authorizeUrl": url, "connectionId": flow_id}


@router.post(
    "/{provider_id}/connection/complete",
    response_model=AccountResponse,
    response_model_by_alias=True,
)
async def complete_connection(
    provider_id: str,
    account: Account | None = Depends(current_session_or_setup),
    code: str = Body(..., embed=True),
    flow_id: str = Body(..., embed=True, alias="connectionId"),
) -> AccountResponse:
    provider = _resolve_provider(provider_id)
    try:
        completed = await provider.connect_complete(flow_id=flow_id, pasted=code)
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
        resolved = account or await Account.get_or_create(completed.provider_email)
    # Runs with no actor execute as the fallback account; claim the slot when
    # none is set yet.
    settings = await UserSettings.get()
    if not settings.fallback_account_id:
        await settings.set_fallback_account(resolved.id)
    login = await ProviderLogin.connect(
        provider=provider.id,
        account=resolved,
        payload=completed.payload,
        expires_at=completed.expires_at,
        provider_email=completed.provider_email,
        kind="oauth",
    )
    # Materialize the reply, then land the login before any provider I/O —
    # an await while flushed rows still hold their locks can stall every other
    # writer on this event loop, and nothing past the point of durability may
    # depend on another database read.
    response = AccountResponse.model_validate(resolved)
    await db_session().commit()
    try:
        # Fresh pickers right after connect for every harness this login
        # serves; fetch failures are tagged inside. The single-use flow is
        # already spent, so trouble here — including a database that vanished
        # under the refresh — only logs.
        for harness in get_harnesses():
            if harness.accepts(login):
                settings = await HarnessSettings.get_registered(harness.name)
                await settings.refresh_models(login)
    except Exception:
        logging.getLogger(__name__).exception("Model refresh after connect failed")
        with suppress(Exception):
            await db_session().rollback()
    return response


@router.post(
    "/{provider_id}/connection",
    response_model=ProviderLoginResponse,
    response_model_by_alias=True,
)
async def connect_key(
    provider_id: str,
    account: Account = Depends(current_session_account),
    key: str = Body(..., embed=True),
) -> ProviderLoginResponse:
    provider = _resolve_provider(provider_id)
    if "api_key" not in provider.login_kinds:
        raise HTTPException(
            status_code=422,
            detail=f"{provider.label} does not accept API keys.",
        )
    if key := key.strip():
        login = await ProviderLogin.connect(
            provider=provider.id,
            account=account,
            payload={"api_key": key},
            expires_at=None,
            provider_email=account.username,
            kind="api_key",
        )
        return ProviderLoginResponse.model_validate(login)
    raise HTTPException(status_code=422, detail="The API key is empty. Paste a key.")


@router.delete("/{provider_id}/connection", status_code=204)
async def disconnect(provider_id: str, account: Account = Depends(current_session_account)) -> None:
    provider = _resolve_provider(provider_id)
    login = await ProviderLogin.get_for_account(provider.id, account.id)
    if login:
        # Only the requesting account's own login — never another's.
        await login.delete()

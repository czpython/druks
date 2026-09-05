import logging
from contextlib import suppress

from fastapi import APIRouter, Body, Depends, HTTPException

from druks.accounts.dependencies import current_session_account, current_session_or_setup
from druks.accounts.models import Account
from druks.accounts.schemas import AccountResponse
from druks.database import db_session
from druks.user_settings.models import UserSettings

from . import directory
from .exceptions import ConnectError
from .models import ProviderCatalog, ProviderKey, ProviderSubscription
from .providers import Provider, get_provider, get_providers, is_registered
from .schemas import (
    ProviderCatalogResponse,
    ProviderDirectoryResponse,
    ProviderKeyResponse,
    ProviderResponse,
    ProviderSubscriptionResponse,
)

router = APIRouter(prefix="/api/providers", tags=["providers"])


@router.get(
    "",
    response_model=list[ProviderResponse],
    response_model_by_alias=True,
    dependencies=[Depends(current_session_or_setup)],
)
async def list_providers() -> tuple[Provider, ...]:
    return get_providers()


@router.get(
    "/subscriptions",
    response_model=list[ProviderSubscriptionResponse],
    response_model_by_alias=True,
)
async def list_subscriptions(
    account: Account = Depends(current_session_account),
) -> list[ProviderSubscription]:
    return await ProviderSubscription.list_for_account(account.id)


@router.get(
    "/keys",
    response_model=list[ProviderKeyResponse],
    response_model_by_alias=True,
    dependencies=[Depends(current_session_account)],
)
async def list_keys() -> list[ProviderKey]:
    return await ProviderKey.list_all()


@router.get(
    "/catalogs",
    response_model=list[ProviderCatalogResponse],
    response_model_by_alias=True,
    dependencies=[Depends(current_session_account)],
)
async def list_catalogs() -> list[ProviderCatalog]:
    return await ProviderCatalog.list_all()


@router.get(
    "/directory",
    response_model=list[ProviderDirectoryResponse],
    response_model_by_alias=True,
    dependencies=[Depends(current_session_account)],
)
async def list_directory() -> list[dict]:
    """The providers an operator can add by key: the directory minus the registered ones."""
    providers = await directory.list_providers()
    return [provider for provider in providers if not is_registered(provider["provider"])]


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
    await ProviderSubscription.connect(
        provider=provider.id,
        account=resolved,
        payload=completed.payload,
        expires_at=completed.expires_at,
        provider_email=completed.provider_email,
    )
    # Materialize the reply, then land the subscription before any provider I/O —
    # an await while flushed rows still hold their locks can stall every other
    # writer on this event loop, and nothing past the point of durability may
    # depend on another database read.
    response = AccountResponse.model_validate(resolved)
    await db_session().commit()
    try:
        # A fresh picker right after connect; fetch failures are tagged inside.
        # The single-use flow is already spent, so trouble here — including a
        # database that vanished under the refresh — only logs.
        await provider.refresh_catalog()
    except Exception:
        logging.getLogger(__name__).exception("Catalog refresh after connect failed")
        with suppress(Exception):
            await db_session().rollback()
    return response


@router.post(
    "/{provider_id}/key",
    response_model=ProviderKeyResponse,
    response_model_by_alias=True,
)
async def create_key(
    provider_id: str,
    account: Account = Depends(current_session_account),
    key: str = Body(..., embed=True),
) -> ProviderKey:
    """The installation's key at a provider. A provider from the directory
    becomes one of the installation's when its key lands."""
    if not (key := key.strip()):
        raise HTTPException(status_code=422, detail="The API key is empty. Paste a key.")
    if is_registered(provider_id):
        provider = get_provider(provider_id)
        if "api_key" not in provider.billing_options:
            raise HTTPException(
                status_code=422, detail=f"{provider.label} does not accept API keys."
            )
        stored = await ProviderKey.create(provider=provider.id, key=key, account=account)
        await provider.refresh_catalog()
        return stored
    try:
        await directory.add_provider(provider_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail=f"Unknown provider: {provider_id!r}") from error
    return await ProviderKey.create(provider=provider_id, key=key, account=account)


@router.delete(
    "/{provider_id}/key", status_code=204, dependencies=[Depends(current_session_account)]
)
async def remove_key(provider_id: str) -> None:
    """A provider the operator added from the directory is gone with its key."""
    stored = await ProviderKey.get(provider_id)
    if stored:
        await stored.delete()
    if is_registered(provider_id):
        return
    catalog = await ProviderCatalog.get(provider_id)
    if not catalog:
        raise HTTPException(status_code=404, detail=f"Unknown provider: {provider_id!r}")
    await catalog.delete()


@router.delete("/{provider_id}/connection", status_code=204)
async def disconnect(provider_id: str, account: Account = Depends(current_session_account)) -> None:
    provider = _resolve_provider(provider_id)
    subscription = await ProviderSubscription.get_for_account(provider.id, account.id)
    if subscription:
        # Only the requesting account's own subscription — never another's.
        await subscription.delete()

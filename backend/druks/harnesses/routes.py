import logging
from contextlib import suppress

from fastapi import APIRouter, Body, Depends, HTTPException

from druks.accounts.dependencies import current_session_account, current_session_or_setup
from druks.accounts.models import Account
from druks.accounts.schemas import AccountResponse
from druks.api.dependencies import SessionDep
from druks.secrets.datastructures import Audience
from druks.secrets.enums import SecretKind
from druks.secrets.models import VaultSecret

from . import directory
from .exceptions import ConnectError
from .models import ProviderCatalog
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
    session: SessionDep,
    account: Account = Depends(current_session_account),
) -> list[VaultSecret]:
    return await VaultSecret.list_subscriptions(
        session, account_id=account.id, include_revoked=True
    )


@router.get(
    "/keys",
    response_model=list[ProviderKeyResponse],
    response_model_by_alias=True,
    dependencies=[Depends(current_session_account)],
)
async def list_keys(session: SessionDep) -> list[VaultSecret]:
    return await VaultSecret.list_keys(session)


@router.get(
    "/catalogs",
    response_model=list[ProviderCatalogResponse],
    response_model_by_alias=True,
    dependencies=[Depends(current_session_account)],
)
async def list_catalogs(session: SessionDep) -> list[ProviderCatalog]:
    return await ProviderCatalog.list_all(session)


@router.get(
    "/directory",
    response_model=list[ProviderDirectoryResponse],
    response_model_by_alias=True,
    dependencies=[Depends(current_session_account)],
)
async def list_directory() -> list[dict]:
    """The providers an operator can add by key."""
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
    # In none/zero the flow starts unbound, and its completion creates the operator.
    url, flow_id = await provider.connect_start(account_id=account.id if account else None)
    return {"authorizeUrl": url, "connectionId": flow_id}


@router.post(
    "/{provider_id}/connection/complete",
    response_model=AccountResponse,
    response_model_by_alias=True,
)
async def complete_connection(
    session: SessionDep,
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
    if account and account.id == completed.account_id:
        resolved = account
    elif completed.account_id:
        # A bound flow never rebinds to another operator.
        raise HTTPException(
            status_code=422,
            detail="This connect was started under a different operator — start it again.",
        )
    else:
        # An unbound flow attaches to this request's account when one exists.
        # get_or_create is atomic, so concurrent completions of one email converge.
        resolved = account or await Account.get_or_create(session, completed.provider_email)
    await VaultSecret.store(
        session,
        SecretKind.SUBSCRIPTION,
        Audience.provider(provider.id),
        account_id=resolved.id,
        secrets=completed.payload,
        identity={"email": completed.provider_email},
        expires_at=completed.expires_at,
    )
    # Build the reply, then commit before provider I/O. Flushed rows hold their locks
    # across an await, and after the commit the reply would need another read.
    response = AccountResponse.model_validate(resolved)
    await session.commit()
    try:
        # The flow is already spent, so a failed refresh only logs.
        await provider.refresh_catalog(session)
    except Exception:
        logging.getLogger(__name__).exception("Catalog refresh after connect failed")
        with suppress(Exception):
            await session.rollback()
    return response


@router.post(
    "/{provider_id}/key",
    response_model=ProviderKeyResponse,
    response_model_by_alias=True,
)
async def create_key(
    provider_id: str,
    session: SessionDep,
    account: Account = Depends(current_session_account),
    key: str = Body(..., embed=True),
) -> VaultSecret:
    """The installation's key at a provider. A key for a directory provider also adds it."""
    if not (key := key.strip()):
        raise HTTPException(status_code=422, detail="The API key is empty. Paste a key.")
    if is_registered(provider_id):
        provider = get_provider(provider_id)
        if "api_key" in provider.billing_options:
            stored = await VaultSecret.paste(
                session, Audience.provider(provider.id), key, pasted_by=account
            )
            await provider.refresh_catalog(session)
            return stored
        raise HTTPException(status_code=422, detail=f"{provider.label} does not accept API keys.")
    try:
        await directory.add_provider(session, provider_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail=f"Unknown provider: {provider_id!r}") from error
    return await VaultSecret.paste(session, Audience.provider(provider_id), key, pasted_by=account)


@router.delete(
    "/{provider_id}/key", status_code=204, dependencies=[Depends(current_session_account)]
)
async def remove_key(session: SessionDep, provider_id: str) -> None:
    """Removing a directory provider's key also removes the provider."""
    if stored := await VaultSecret.lookup(
        session, SecretKind.STATIC, Audience.provider(provider_id)
    ):
        await stored.revoke("user")
    if is_registered(provider_id):
        return
    if catalog := await session.get(ProviderCatalog, provider_id):
        await catalog.delete()
        return
    raise HTTPException(status_code=404, detail=f"Unknown provider: {provider_id!r}")


@router.delete("/{provider_id}/connection", status_code=204)
async def disconnect(
    session: SessionDep, provider_id: str, account: Account = Depends(current_session_account)
) -> None:
    provider = _resolve_provider(provider_id)
    if subscription := await VaultSecret.lookup(
        session, SecretKind.SUBSCRIPTION, Audience.provider(provider.id), account.id
    ):
        await subscription.revoke("user")

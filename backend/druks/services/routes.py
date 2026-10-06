from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from druks.accounts.context import current_account_id
from druks.accounts.dependencies import current_session_account
from druks.api.dependencies import SessionDep
from druks.apps.registry import services
from druks.core.templates import render_page
from druks.secrets.datastructures import Audience
from druks.secrets.enums import SecretKind
from druks.secrets.models import VaultSecret
from druks.services.exceptions import (
    OauthExchangeError,
    OauthPageError,
    ServiceConnectError,
    ServiceManagedError,
    ServiceNotConnectedError,
)
from druks.services.oauth import OauthClient, complete_connect
from druks.services.schemas import ConnectionResponse, ServiceResponse
from druks.settings import load_settings
from druks.signals import publish

router = APIRouter(prefix="/api/services", tags=["services"])
oauth_router = APIRouter(prefix="/api/oauth", tags=["oauth"])


@router.get("", response_model=list[ServiceResponse], response_model_by_alias=True)
async def list_services(session: SessionDep) -> list[ServiceResponse]:
    entries = []
    managed_by = load_settings().manager.name
    account_connections = await VaultSecret.list_owned_by(session, current_account_id.get())
    for service in services.all():
        audience = Audience.service(service.slug)
        row = await VaultSecret.lookup(session, service.secret_kind, audience)
        connections = [
            connection for connection in account_connections if connection.audience == audience
        ]
        install_url = await service.get_install_endpoint() if row else ""
        if install_url and service.is_managed():
            # The manager binds the installation, so the install starts at the sign-in door.
            install_url = f"/api/oauth/{service.slug}/connect?install=1"
        entries.append(
            ServiceResponse.from_row(
                service,
                row,
                connections,
                managed=service.is_configured(),
                managed_by=managed_by,
                install_url=install_url,
            )
        )
    return entries


# Session identity only: an agent PAT never writes the appliance's own credentials.
@router.post(
    "/{slug}",
    response_model=ServiceResponse,
    response_model_by_alias=True,
    dependencies=[Depends(current_session_account)],
)
async def connect_service(slug: str, payload: dict[str, str]) -> ServiceResponse:
    service = services.get(slug)
    if not service:
        raise HTTPException(status_code=404, detail=f"No service {slug!r}.")
    try:
        row = await service.connect(payload)
    except ServiceConnectError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return ServiceResponse.from_row(service, row)


@router.delete("/{slug}", status_code=204, dependencies=[Depends(current_session_account)])
async def disconnect_service(session: SessionDep, slug: str) -> None:
    service = services.get(slug)
    if not service:
        raise HTTPException(status_code=404, detail=f"No service {slug!r}.")
    if service.is_configured():
        raise ServiceManagedError(slug)
    await service.disconnect(session)


@oauth_router.get("/{slug}/connect", dependencies=[Depends(current_session_account)])
async def connect_oauth_service(
    session: SessionDep,
    slug: str,
    request: Request,
    connection: str = "",
    next: str = "",
    install: bool = False,
) -> RedirectResponse:
    """Start a sign-in at the provider. ``install`` starts it from the provider's
    install page, so the callback also names the installation."""
    service = services.get(slug)
    if not service or not service.token_endpoint:
        raise OauthPageError(f"No OAuth service {slug!r}.", status_code=404)
    account_id = current_account_id.get()
    if connection:
        row = await session.get(VaultSecret, connection)
        if not row or row.audience != Audience.service(slug) or row.account_id != account_id:
            raise OauthPageError(f"No connection {connection!r} on {slug!r}.", status_code=404)
    if next and (not next.startswith("/") or next.startswith(("//", "/\\"))):
        # A bare same-origin path only — anything host-shaped is an open redirect.
        raise OauthPageError("next must be a path starting with '/'.", status_code=422)
    endpoint = request.app.state.settings.urls.endpoint
    if not endpoint:
        raise OauthPageError(
            "The provider redirects the operator's browser back to druks. "
            "Set urls.endpoint to the address druks has in that browser.",
            status_code=409,
        )
    try:
        client = await service.get_oauth_client()
    except ServiceNotConnectedError as error:
        raise OauthPageError(str(error), status_code=409) from error
    authorization_endpoint = ""
    if install:
        authorization_endpoint = await service.get_install_endpoint()
        if not authorization_endpoint:
            raise OauthPageError(f"{slug} has no install page.", status_code=404)
    scopes = service.scopes()
    url = await client.begin_connect(
        redirect_uri=client.redirect_uri or f"{endpoint.rstrip('/')}/api/oauth/callback",
        scopes=scopes,
        consent_query=service.get_consent_query(scopes),
        context={"account_id": account_id, "connection_id": connection, "next": next},
        authorization_endpoint=authorization_endpoint,
    )
    return RedirectResponse(url)


@oauth_router.get(
    "/callback",
    response_class=HTMLResponse,
    dependencies=[Depends(current_session_account)],
)
async def oauth_callback(
    session: SessionDep,
    state: str = "",
    code: str = "",
    error: str = "",
    installation_id: str = "",
) -> Response:
    if error:
        raise OauthPageError(
            f"The authorization server denied the request: {error}", status_code=400
        )
    if not state or not code:
        raise OauthPageError("Missing state or code in the callback.", status_code=400)
    try:
        tokens, pending = await complete_connect(
            state=state, code=code, installation_id=installation_id
        )
    except OauthExchangeError as exchange_error:
        raise OauthPageError(str(exchange_error), status_code=400) from exchange_error
    provider = pending["provider"]
    service = services.get(provider)
    if not service:
        # A state begun by another door (an MCP connect) finishes at its own callback.
        raise OauthPageError(f"No OAuth service {provider!r}.", status_code=400)
    try:
        grant = service.read_grant(tokens)
    except OauthExchangeError as exchange_error:
        raise OauthPageError(str(exchange_error), status_code=400) from exchange_error
    if installation_id:
        # Learn the installation before the grant is stored, so a failure leaves nothing
        # half done.
        await service.sync_installations()
    granted = grant["scopes"] or pending["scopes"]
    identity = await service.get_identity(grant["access_token"])
    # A grant refreshes through its refresh token. Without one, the access token
    # never expires and is the grant itself.
    refresh_token = grant["refresh_token"]
    kept = {} if refresh_token else {"access_token": grant["access_token"]}
    connection_id = pending["connection_id"]
    # Reconsent names an existing row by id; a declared identity key
    # matches a fresh sign-in to one. Both make a revoked row live again.
    row = None
    if connection_id:
        row = await session.get(VaultSecret, connection_id)
        if not row:
            raise OauthPageError(
                "The connection was removed while consent was open.", status_code=400
            )
    elif service.identity_key and (value := identity.get(service.identity_key)):
        row = await VaultSecret.get_for_identity(
            session, Audience.service(provider), pending["account_id"], service.identity_key, value
        )
    reconsent = bool(row)
    if row:
        await row.reconnect(
            refresh_token=refresh_token, scopes=granted, identity=identity, secrets=kept
        )
        # A token cached before this consent must not serve the new one.
        await OauthClient(provider=provider).evict_access_token(row.id)
    else:
        row = await VaultSecret.connect(
            session,
            Audience.service(provider),
            account_id=pending["account_id"],
            refresh_token=refresh_token,
            scopes=granted,
            identity=identity,
            secrets=kept,
        )
    await publish(
        "oauth.connected",
        provider=provider,
        connection_id=row.id,
        account_id=row.account_id,
        reconsent=reconsent,
    )
    if pending["next"]:
        # The request's session commits after the response leaves, and the browser
        # follows a redirect at once: the next page must find the grant.
        await session.commit()
        return RedirectResponse(pending["next"])
    return render_page("service_oauth_callback.html", slug=provider)


@oauth_router.get(
    "/connections",
    dependencies=[Depends(current_session_account)],
    response_model=list[ConnectionResponse],
    response_model_by_alias=True,
)
async def list_connections(session: SessionDep) -> list[VaultSecret]:
    return await VaultSecret.list_owned_by(session, current_account_id.get())


@oauth_router.delete(
    "/connections/{connection_id}",
    status_code=204,
    dependencies=[Depends(current_session_account)],
)
async def disconnect_connection(session: SessionDep, connection_id: str) -> None:
    row = await session.get(VaultSecret, connection_id)
    if not row or row.kind != SecretKind.OAUTH or row.account_id != current_account_id.get():
        raise HTTPException(status_code=404, detail=f"No connection {connection_id!r}.")
    # Revoking is idempotent: a second delete finds the connection revoked.
    if row.is_live:
        await OauthClient(provider=row.audience_name).disconnect(row, reason="user")

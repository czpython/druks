from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from druks.accounts.context import current_account_id
from druks.accounts.dependencies import current_session_account
from druks.core.templates import render_page
from druks.extensions.registry import services
from druks.services.exceptions import (
    OauthExchangeError,
    OauthPageError,
    ServiceConnectError,
    ServiceNotConnectedError,
)
from druks.services.models import OauthConnection, ServiceIdentity
from druks.services.oauth import OauthClient, complete_connect
from druks.services.schemas import ConnectionResponse, ServiceResponse
from druks.signals import publish

router = APIRouter(prefix="/api/services", tags=["services"])
oauth_router = APIRouter(prefix="/api/oauth", tags=["oauth"])


@router.get("", response_model=list[ServiceResponse], response_model_by_alias=True)
async def list_services() -> list[ServiceResponse]:
    entries = []
    for service in services.all():
        try:
            row = ServiceIdentity.get(service.slug)
        except ServiceNotConnectedError:
            row = None
        connections = []
        if service.token_endpoint:
            # The detail shows revoked connections as history beside the live.
            connections = OauthConnection.list_for_provider(service.slug, include_revoked=True)
        entries.append(ServiceResponse.from_row(service, row, connections))
    return entries


# Session identity only, like the settings PATCH: the appliance's own
# credentials are never writable with an agent PAT.
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
    if service.token_endpoint:
        # A replaced client can never refresh the old client's connections —
        # revoke every live one; the consents stay on record.
        client = OauthClient(provider=slug)
        for connection in OauthConnection.list_for_provider(slug):
            await client.disconnect(connection, reason="client_replaced")
            await publish(
                "oauth.disconnected",
                provider=slug,
                connection_id=connection.id,
                account_id=connection.account_id,
            )
    return ServiceResponse.from_row(service, row)


def _get_oauth_service(slug: str):
    service = services.get(slug)
    if not service or not service.token_endpoint:
        raise OauthPageError(f"No OAuth service {slug!r}.", status_code=404)
    return service


@oauth_router.get("/{slug}/connect", dependencies=[Depends(current_session_account)])
async def connect_oauth_service(
    slug: str, request: Request, connection: str = "", next: str = ""
) -> RedirectResponse:
    service = _get_oauth_service(slug)
    account_id = current_account_id.get()
    if connection:
        row = OauthConnection.get(connection)
        if not row or row.provider != slug:
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
        client = service.get_oauth_client()
    except ServiceNotConnectedError as error:
        raise OauthPageError(str(error), status_code=409) from error
    url = await client.begin_connect(
        redirect_uri=f"{endpoint.rstrip('/')}/api/oauth/callback",
        scopes=service.required_scopes(),
        context={"account_id": account_id, "connection_id": connection, "next": next},
    )
    return RedirectResponse(url)


@oauth_router.get("/callback", response_class=HTMLResponse)
async def oauth_callback(state: str = "", code: str = "", error: str = "") -> Response:
    if error:
        raise OauthPageError(
            f"The authorization server denied the request: {error}", status_code=400
        )
    if not state or not code:
        raise OauthPageError("Missing state or code in the callback.", status_code=400)
    try:
        tokens, pending = await complete_connect(state=state, code=code)
    except OauthExchangeError as exchange_error:
        raise OauthPageError(str(exchange_error), status_code=400) from exchange_error
    provider = pending["provider"]
    service = services.get(provider)
    if not service:
        # A state begun by another door (an MCP connect) finishes at its own callback.
        raise OauthPageError(f"No OAuth service {provider!r}.", status_code=400)
    granted = tokens.get("scope", "").split() or pending["scopes"]
    identity = await service.get_identity(tokens["access_token"])
    connection_id = pending["connection_id"]
    # Reconsent names an existing row by id; a declared identity key
    # matches a fresh sign-in to one. Both make a revoked row live again.
    row = None
    if connection_id:
        row = OauthConnection.get(connection_id)
        if not row:
            raise OauthPageError(
                "The connection was removed while consent was open.", status_code=400
            )
    elif service.identity_key and (value := identity.get(service.identity_key)):
        row = OauthConnection.get_for_identity(
            provider, pending["account_id"], service.identity_key, value
        )
    reconsent = bool(row)
    if row:
        row.reconnect(refresh_token=tokens["refresh_token"], scopes=granted, identity=identity)
        # A token cached before this consent must not serve the new one.
        await OauthClient(provider=provider).evict_access_token(row.id)
    else:
        row = OauthConnection.create(
            provider=provider,
            account_id=pending["account_id"],
            refresh_token=tokens["refresh_token"],
            scopes=granted,
            identity=identity,
        )
    await publish(
        "oauth.connected",
        provider=provider,
        connection_id=row.id,
        account_id=row.account_id,
        reconsent=reconsent,
    )
    if pending["next"]:
        return RedirectResponse(pending["next"])
    return render_page("service_oauth_callback.html", slug=provider)


@oauth_router.get("/connections", dependencies=[Depends(current_session_account)])
async def list_connections() -> list[ConnectionResponse]:
    rows = OauthConnection.list_owned_by(current_account_id.get())
    return [ConnectionResponse.model_validate(row) for row in rows]


@oauth_router.delete(
    "/connections/{connection_id}",
    status_code=204,
    dependencies=[Depends(current_session_account)],
)
async def disconnect_connection(connection_id: str) -> None:
    row = OauthConnection.get(connection_id)
    if not row:
        raise HTTPException(status_code=404, detail=f"No connection {connection_id!r}.")
    if row.revoked_at:
        # Revoking is idempotent — the second delete finds the state true.
        return
    await OauthClient(provider=row.provider).disconnect(row, reason="user")
    await publish(
        "oauth.disconnected",
        provider=row.provider,
        connection_id=row.id,
        account_id=row.account_id,
    )

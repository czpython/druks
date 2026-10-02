import json
from typing import Annotated

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from druks.accounts.context import current_account_id
from druks.accounts.dependencies import current_session_account
from druks.api.dependencies import SessionDep
from druks.apps.registry import mcp_servers
from druks.core.templates import render_page
from druks.mcp import oauth
from druks.mcp.constants import HEADER_NAME_PATTERN
from druks.mcp.enums import Credential, IdentityMode
from druks.mcp.exceptions import (
    InvalidServerNameError,
    McpServerNotFoundError,
    OauthConnectError,
    ReservedServerNameError,
)
from druks.mcp.helpers import get_grant_account
from druks.mcp.models import McpServer
from druks.mcp.schemas import (
    ConnectMcpServerResponse,
    CreateMcpServerRequest,
    McpServerConnectionResponse,
    McpServerDirectoryResponse,
    McpServerResponse,
)
from druks.secrets.models import VaultSecret

router = APIRouter(prefix="/api/mcp-servers", tags=["mcp-servers"])


async def _response(session: AsyncSession, name: str) -> McpServerResponse:
    access = await McpServer.get_access(session, name, current_account_id.get())
    return McpServerResponse.model_validate(access)


@router.get("", response_model=list[McpServerResponse])
async def list_mcp_servers(session: SessionDep) -> list[McpServerResponse]:
    return [
        McpServerResponse.model_validate(access)
        for access in await McpServer.list_access(session, current_account_id.get())
    ]


@router.get("/directory", response_model=list[McpServerDirectoryResponse])
async def list_mcp_server_directory(request: Request) -> list[dict]:
    directory = json.loads(request.app.state.settings.mcp_directory_path.read_text())
    return [{"name": name, **entry} for name, entry in directory.items()]


@router.post("", response_model=McpServerResponse)
async def add_mcp_server(session: SessionDep, body: CreateMcpServerRequest) -> McpServerResponse:
    if body.name in mcp_servers:
        raise HTTPException(
            status_code=409,
            detail=f"MCP server {body.name!r} is built-in; configure it instead of adding it.",
        )
    if await McpServer.get_for_name(session, body.name):
        raise HTTPException(
            status_code=409, detail=f"MCP server {body.name!r} already exists; remove it first."
        )
    # A custom server is delivered enabled, so a blank url (an unreachable
    # endpoint) or missing auth (unauthenticated) would break every agent VM.
    # Reject here rather than persist a row that fails at delivery.
    if not body.url.strip():
        raise HTTPException(status_code=422, detail=f"MCP server {body.name!r} needs a url.")
    secret_headers = {name.strip(): value for name, value in body.secret_headers.items()}
    if secret_headers and all(
        HEADER_NAME_PATTERN.match(header) and value.strip()
        for header, value in secret_headers.items()
    ):
        try:
            await McpServer.create(
                session, name=body.name, url=body.url, secret_headers=secret_headers
            )
        except (InvalidServerNameError, ReservedServerNameError) as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return await _response(session, body.name)
    raise HTTPException(
        status_code=422,
        detail=f"MCP server {body.name!r} needs a secret header with a valid name and a value.",
    )


@router.post("/directory", response_model=McpServerResponse)
async def add_directory_mcp_server(
    session: SessionDep, request: Request, name: str = Body(embed=True)
) -> McpServerResponse:
    if name in mcp_servers:
        raise HTTPException(
            status_code=409,
            detail=f"MCP server {name!r} is built-in; configure it instead of adding it.",
        )
    if await McpServer.get_for_name(session, name):
        raise HTTPException(
            status_code=409, detail=f"MCP server {name!r} already exists; remove it first."
        )
    directory = json.loads(request.app.state.settings.mcp_directory_path.read_text())
    if name in directory:
        # Every directory server signs in with OAuth, so it ships dark until its
        # Connect lands.
        await McpServer.create(
            session, name=name, url=directory[name]["url"], is_oauth=True, is_enabled=False
        )
        return await _response(session, name)
    raise HTTPException(status_code=404, detail=f"{name!r} is not in the MCP server directory.")


@router.patch("/{name}", response_model=McpServerResponse)
async def set_mcp_server_enabled(
    session: SessionDep, name: str, is_enabled: bool = Body(embed=True)
) -> McpServerResponse:
    try:
        await McpServer.set_enabled(session, name, is_enabled)
    except McpServerNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    return await _response(session, name)


@router.get("/{name}/connections", response_model=list[McpServerConnectionResponse])
async def list_mcp_server_connections(session: SessionDep, name: str) -> list[VaultSecret]:
    if not await McpServer.get_for_name(session, name):
        raise HTTPException(status_code=404, detail=f"MCP server {name!r} not found")
    return await oauth.list_connections(session, name)


@router.delete("/{name}", status_code=204)
async def remove_mcp_server(session: SessionDep, name: str) -> None:
    if name in mcp_servers:
        # A built-in is druks-owned — removing it would silently drop it from
        # every agent VM; disable it instead if unwanted.
        raise HTTPException(
            status_code=409, detail=f"MCP server {name!r} is managed by druks; disable it instead."
        )
    server = await McpServer.get_for_name(session, name)
    if not server:
        raise HTTPException(status_code=404, detail=f"MCP server {name!r} not found")
    # Revoke before the server row goes — the registration lookup needs it.
    for connection in await oauth.list_connections(session, name):
        await oauth.disconnect(session, name, connection.account_id, reason="server_removed")
    await server.delete()


@router.post("/{name}/connect", response_model=ConnectMcpServerResponse)
async def connect_mcp_server(
    session: SessionDep,
    name: str,
    request: Request,
    identity_mode: Annotated[IdentityMode, Body(embed=True)],
) -> ConnectMcpServerResponse:
    access = await McpServer.get_access(session, name, current_account_id.get())
    if not access or not access.is_oauth:
        raise HTTPException(status_code=404, detail=f"MCP server {name!r} is not an OAuth server.")
    if await oauth.list_connections(session, name) and access.identity_mode != identity_mode:
        raise HTTPException(
            status_code=409,
            detail=f"MCP server {name!r} already uses {access.identity_mode!r} identity.",
        )
    endpoint = request.app.state.settings.urls.endpoint
    if not endpoint:
        # The authorization server redirects the operator's browser back to
        # druks, so the flow needs the address that browser reaches druks at.
        raise HTTPException(
            status_code=409,
            detail="Set urls.endpoint to the base URL the operator's browser reaches druks "
            "at, to connect OAuth MCP servers.",
        )
    if access.credential == Credential.SERVICE_CONNECTION:
        # The account's sign-in at the service is its credential here, and a
        # sign-in belongs to one account. The service's callback knows nothing of
        # this server, so Connect is where it becomes enabled.
        if identity_mode == IdentityMode.PER_USER:
            await McpServer.set_enabled(session, name, is_enabled=True)
            return ConnectMcpServerResponse(
                authorization_url=f"{endpoint.rstrip('/')}/api/oauth/{access.service}"
                "/connect?next=/settings/mcp"
            )
        raise HTTPException(
            status_code=409,
            detail=f"MCP server {name!r} signs in through {access.service!r}, per account.",
        )
    try:
        authorization_url = await oauth.begin_connect(
            name,
            access.url,
            endpoint,
            account_id=current_account_id.get(),
            identity_mode=identity_mode,
        )
    except OauthConnectError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error
    return ConnectMcpServerResponse(authorization_url=authorization_url)


@router.get(
    "/oauth/callback",
    response_class=HTMLResponse,
    dependencies=[Depends(current_session_account)],
)
async def oauth_callback(
    session: SessionDep, state: str = "", code: str = "", error: str = ""
) -> HTMLResponse:
    # The operator's browser lands here from the consent screen — a human-facing
    # page, not a JSON API. Failures surface as loud HTTP errors (the app's
    # handler renders them); success tells them to close the tab.
    if error:
        raise HTTPException(
            status_code=400, detail=f"The authorization server denied the request: {error}"
        )
    if not state or not code:
        raise HTTPException(status_code=400, detail="Missing state or code in the callback.")
    try:
        name = await oauth.complete_connect(session, state=state, code=code)
    except OauthConnectError as exchange_error:
        raise HTTPException(status_code=400, detail=str(exchange_error)) from exchange_error
    # Connecting is the operator's explicit "use this server" — a
    # connected-but-disabled server is a dead end nobody asks for.
    await McpServer.set_enabled(session, name, is_enabled=True)
    # druks opened this tab via window.open, so the page may close itself; the
    # broadcast tells the settings page to refetch before the tab goes. The
    # text stays for browsers that refuse the close.
    return render_page("mcp_oauth_callback.html", name=name)


@router.delete("/{name}/grant", status_code=204)
async def disconnect_mcp_server(session: SessionDep, name: str) -> None:
    access = await McpServer.get_access(session, name, current_account_id.get())
    if not access or not access.is_oauth:
        raise HTTPException(status_code=404, detail=f"MCP server {name!r} is not an OAuth server.")
    if not access.identity_mode:
        raise HTTPException(status_code=404, detail=f"MCP server {name!r} has no grant.")
    account_id = get_grant_account(access.identity_mode, current_account_id.get())
    connection = await oauth.get_connection(session, name, account_id)
    if not connection:
        raise HTTPException(
            status_code=404,
            detail=f"MCP server {name!r} has no grant for account {account_id!r}.",
        )
    await oauth.disconnect(session, name, account_id)
    if not await oauth.list_connections(session, name):
        # The last grant leaving reopens the mode choice: the next connect is
        # a first connect again.
        server = await McpServer.get_for_name(session, name)
        server.identity_mode = None
    if access.identity_mode == IdentityMode.SHARED:
        await McpServer.set_enabled(session, name, is_enabled=False)

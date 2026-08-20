import json
from typing import Annotated

from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import HTMLResponse

from druks.accounts.context import current_account_id
from druks.core.templates import render_page
from druks.extensions.registry import mcp_servers
from druks.mcp import oauth, registry
from druks.mcp.enums import IdentityMode, TokenSource
from druks.mcp.exceptions import (
    InvalidServerNameError,
    OauthConnectError,
    RegistryUnavailableError,
)
from druks.mcp.helpers import get_grant_account
from druks.mcp.models import McpServer
from druks.mcp.schemas import (
    ConnectMcpServerResponse,
    CreateMcpServerRequest,
    InstallMcpServerRequest,
    McpRegistryCandidateResponse,
    McpServerResponse,
)

router = APIRouter(prefix="/api/mcp-servers", tags=["mcp-servers"])


def _response(name: str) -> McpServerResponse:
    return McpServerResponse.model_validate(McpServer.get_resolved(current_account_id.get())[name])


@router.get("", response_model=list[McpServerResponse])
async def list_mcp_servers() -> list[McpServerResponse]:
    return [
        McpServerResponse.model_validate(server)
        for server in McpServer.get_resolved(current_account_id.get()).values()
    ]


@router.get("/registry", response_model=list[McpRegistryCandidateResponse])
async def search_mcp_registry(query: str, request: Request) -> list[McpRegistryCandidateResponse]:
    pins = json.loads(request.app.state.settings.mcp_trusted_path.read_text())
    try:
        entries = await registry.search_registry(query)
    except RegistryUnavailableError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error
    candidates = registry.resolve_candidates(entries, pins)
    return [
        McpRegistryCandidateResponse.model_validate(candidate) for candidate in candidates.values()
    ]


@router.post("", response_model=McpServerResponse)
async def add_mcp_server(body: CreateMcpServerRequest) -> McpServerResponse:
    if body.name in mcp_servers:
        raise HTTPException(
            status_code=409,
            detail=f"MCP server {body.name!r} is built-in; configure it instead of adding it.",
        )
    if McpServer.get_for_name(body.name):
        raise HTTPException(
            status_code=409, detail=f"MCP server {body.name!r} already exists; remove it first."
        )
    # A custom server is delivered enabled, so a blank url (an unreachable
    # endpoint) or a blank token (unauthenticated) would break every agent VM.
    # Reject both here rather than persist a row that fails at delivery.
    if not body.url.strip():
        raise HTTPException(status_code=422, detail=f"MCP server {body.name!r} needs a url.")
    if not body.token.strip():
        raise HTTPException(
            status_code=422, detail=f"MCP server {body.name!r} needs a bearer token."
        )
    try:
        McpServer.create(name=body.name, url=body.url, token=body.token)
    except InvalidServerNameError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return _response(body.name)


@router.post("/registry", response_model=McpServerResponse)
async def install_mcp_server(body: InstallMcpServerRequest, request: Request) -> McpServerResponse:
    if body.name in mcp_servers:
        raise HTTPException(
            status_code=409,
            detail=f"MCP server {body.name!r} is built-in; configure it instead of adding it.",
        )
    if McpServer.get_for_name(body.name):
        raise HTTPException(
            status_code=409, detail=f"MCP server {body.name!r} already exists; remove it first."
        )
    # url, auth shape and header secrecy come from the re-resolved registry
    # entry, never the client.
    pins = json.loads(request.app.state.settings.mcp_trusted_path.read_text())
    try:
        entries = await registry.search_registry(body.registry)
    except RegistryUnavailableError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error
    candidate = registry.resolve_candidates(entries, pins).get(body.registry)
    if not candidate:
        raise HTTPException(
            status_code=404,
            detail=f"Registry entry {body.registry!r} is not installable over HTTP.",
        )
    declared = {spec["name"] for spec in candidate["headers"]}
    unknown = sorted(set(body.headers) - declared)
    if unknown:
        raise HTTPException(
            status_code=422,
            detail=f"{body.registry!r} declares no header(s): {', '.join(unknown)}.",
        )
    filled = {}
    for header, value in body.headers.items():
        if stripped := value.strip():
            filled[header] = stripped
    required = {spec["name"] for spec in candidate["headers"] if spec.get("isRequired")}
    missing = sorted(required - set(filled))
    if missing:
        raise HTTPException(
            status_code=422, detail=f"Missing required header value(s): {', '.join(missing)}."
        )
    secret = {spec["name"] for spec in candidate["headers"] if spec.get("isSecret")}
    if secret:
        # A secret declared header carries the auth itself — no bearer.
        token_source = ""
        is_enabled = True
    else:
        # OAuth: ships dark until its Connect lands.
        token_source = TokenSource.OAUTH
        is_enabled = False
    try:
        McpServer.create(
            name=body.name,
            url=candidate["url"],
            token_source=token_source,
            headers={h: v for h, v in filled.items() if h not in secret},
            secret_headers={h: v for h, v in filled.items() if h in secret},
            is_enabled=is_enabled,
        )
    except InvalidServerNameError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return _response(body.name)


@router.patch("/{name}", response_model=McpServerResponse)
async def set_mcp_server_enabled(
    name: str, is_enabled: bool = Body(embed=True)
) -> McpServerResponse:
    if not McpServer.set_enabled(name, is_enabled):
        raise HTTPException(status_code=404, detail=f"MCP server {name!r} not found")
    return _response(name)


@router.delete("/{name}", status_code=204)
async def remove_mcp_server(name: str) -> None:
    if name in mcp_servers:
        # A built-in is druks-owned — removing it would silently drop it from
        # every agent VM; disable it instead if unwanted.
        raise HTTPException(
            status_code=409, detail=f"MCP server {name!r} is managed by druks; disable it instead."
        )
    server = McpServer.get_for_name(name)
    if not server:
        raise HTTPException(status_code=404, detail=f"MCP server {name!r} not found")
    connections = oauth.list_connections(name)
    server.delete()
    for connection in connections:
        await oauth.evict_access_token(name, connection.account_id)
        connection.delete()


@router.post("/{name}/connect", response_model=ConnectMcpServerResponse)
async def connect_mcp_server(
    name: str,
    request: Request,
    identity_mode: Annotated[IdentityMode, Body(embed=True)],
) -> ConnectMcpServerResponse:
    server = McpServer.get_resolved(current_account_id.get()).get(name)
    if not server or server["token_source"] != TokenSource.OAUTH:
        raise HTTPException(status_code=404, detail=f"MCP server {name!r} is not an OAuth server.")
    if oauth.list_connections(name) and server["identity_mode"] != identity_mode:
        raise HTTPException(
            status_code=409,
            detail=f"MCP server {name!r} already uses {server['identity_mode']!r} identity.",
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
    try:
        authorization_url = await oauth.begin_connect(
            name,
            server["url"],
            endpoint,
            account_id=current_account_id.get(),
            identity_mode=identity_mode,
        )
    except OauthConnectError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error
    return ConnectMcpServerResponse(authorization_url=authorization_url)


@router.get("/oauth/callback", response_class=HTMLResponse)
async def oauth_callback(state: str = "", code: str = "", error: str = "") -> HTMLResponse:
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
        name = await oauth.complete_connect(state=state, code=code)
    except OauthConnectError as exchange_error:
        raise HTTPException(status_code=400, detail=str(exchange_error)) from exchange_error
    # Connecting is the operator's explicit "use this server" — a
    # connected-but-disabled server is a dead end nobody asks for.
    McpServer.set_enabled(name, is_enabled=True)
    # druks opened this tab via window.open, so the page may close itself; the
    # broadcast tells the settings modal to refetch before the tab goes. The
    # text stays for browsers that refuse the close.
    return render_page("mcp_oauth_callback.html", name=name)


@router.delete("/{name}/grant", status_code=204)
async def disconnect_mcp_server(name: str) -> None:
    server = McpServer.get_resolved(current_account_id.get()).get(name)
    if not server or server["token_source"] != TokenSource.OAUTH:
        raise HTTPException(status_code=404, detail=f"MCP server {name!r} is not an OAuth server.")
    if not server["identity_mode"]:
        raise HTTPException(status_code=404, detail=f"MCP server {name!r} has no grant.")
    account_id = get_grant_account(server["identity_mode"], current_account_id.get())
    connection = oauth.get_connection(name, account_id)
    if not connection:
        raise HTTPException(
            status_code=404,
            detail=f"MCP server {name!r} has no grant for account {account_id!r}.",
        )
    await oauth.disconnect(name, account_id)
    if not oauth.list_connections(name):
        # The last grant leaving reopens the mode choice: the next connect is
        # a first connect again.
        server_row = McpServer.get_for_name(name)
        if server_row:
            server_row.identity_mode = None
    if server["identity_mode"] == IdentityMode.SHARED:
        McpServer.set_enabled(name, is_enabled=False)

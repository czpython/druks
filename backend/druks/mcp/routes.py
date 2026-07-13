from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import HTMLResponse

from druks.extensions.registry import mcp_servers
from druks.mcp import oauth
from druks.mcp.enums import TokenSource
from druks.mcp.exceptions import InvalidServerNameError, OauthConnectError
from druks.mcp.models import McpOauthGrant, McpServer
from druks.mcp.schemas import (
    ConnectMcpServerResponse,
    CreateMcpServerRequest,
    McpServerResponse,
)

router = APIRouter(prefix="/api/mcp-servers", tags=["mcp-servers"])


def _response(name: str) -> McpServerResponse:
    return McpServerResponse.model_validate(
        next(s for s in McpServer.list_resolved() if s["name"] == name)
    )


@router.get("", response_model=list[McpServerResponse])
async def list_mcp_servers() -> list[McpServerResponse]:
    return [McpServerResponse.model_validate(server) for server in McpServer.list_resolved()]


@router.post("", response_model=McpServerResponse)
async def add_mcp_server(body: CreateMcpServerRequest) -> McpServerResponse:
    if body.name in mcp_servers:
        raise HTTPException(
            status_code=409,
            detail=f"MCP server {body.name!r} is built-in; configure it instead of adding it.",
        )
    if McpServer.get_by_name(body.name):
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
    server = McpServer.get_by_name(name)
    if not server:
        raise HTTPException(status_code=404, detail=f"MCP server {name!r} not found")
    server.delete()


@router.post("/{name}/connect", response_model=ConnectMcpServerResponse)
async def connect_mcp_server(name: str, request: Request) -> ConnectMcpServerResponse:
    definition = mcp_servers.get(name)
    if not definition or definition["token_source"] != TokenSource.OAUTH:
        raise HTTPException(status_code=404, detail=f"MCP server {name!r} is not an OAuth server.")
    endpoint = request.app.state.settings.endpoint
    if not endpoint:
        # The authorization server redirects the operator's browser back to
        # druks, so the flow needs the address that browser reaches druks at.
        raise HTTPException(
            status_code=409,
            detail="Set DRUKS_ENDPOINT to the base URL the operator's browser reaches druks "
            "at, to connect OAuth MCP servers.",
        )
    try:
        authorization_url = await oauth.begin_connect(name, definition["url"], endpoint)
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
    McpServer.set_enabled(name, True)
    # druks opened this tab via window.open, so the page may close itself; the
    # broadcast tells the settings modal to refetch before the tab goes. The
    # text stays for browsers that refuse the close.
    return HTMLResponse(
        f"<html><body><p>Connected MCP server <b>{name}</b>. "
        "You can close this tab and return to druks.</p>"
        f"<script>new BroadcastChannel('druks-mcp-connect').postMessage({name!r});"
        "window.close()</script></body></html>"
    )


@router.delete("/{name}/grant", status_code=204)
async def disconnect_mcp_server(name: str) -> None:
    grant = McpOauthGrant.get_by_server(name)
    if not grant:
        raise HTTPException(status_code=404, detail=f"MCP server {name!r} has no grant.")
    grant.delete()
    await oauth.evict_access_token(name)
    # The mirror of connect-enables: a disconnected OAuth server can't serve
    # a single call, so leaving it enabled just ships a dead entry to VMs.
    McpServer.set_enabled(name, False)

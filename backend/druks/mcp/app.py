# The inbound /mcp endpoint ("server" stays reserved for the registry rows).
# Its tools are derived from the routes tagged "agent": the route is an
# operation's single declaration — schema, docstring, operation_id — and a
# tagged extension route joins the surface the same way.
from collections.abc import Generator

import httpx
from fastapi import FastAPI
from fastmcp import FastMCP
from fastmcp.server.auth import AccessToken, TokenVerifier
from fastmcp.server.dependencies import get_http_request
from fastmcp.server.http import StarletteWithLifespan
from fastmcp.server.providers.openapi import MCPType, OpenAPIProvider, OpenAPITool, RouteMap
from mcp.types import ToolAnnotations

from druks.accounts.exceptions import InvalidPatError
from druks.accounts.models import PersonalAccessToken
from druks.database import db_session

_INSTRUCTIONS = """\
Druks coordinates durable agent runs over shared work items. This surface
answers gates: get_gate returns a parked run's ask, a bounded artifact
chunk, and parkedAt; answer_gate must echo that parkedAt unchanged — it
names the exact question being answered, and a repeat answer reports
already_answered. get_agent_call returns bounded transcript and stderr
tails, never full payloads. cancel_run records its reason as the run's
failure. get_usage is the caller's quota and today's spend. There is no
push channel; poll. Tool failures embed {code, message, retryable} from the
HTTP surface.
"""

# FastMCP logs component-fn errors instead of raising, so the tools/list
# test is the guard: an unmapped tagged route ships unannotated and fails CI.
_TOOL_ANNOTATIONS = {
    "get_gate": ToolAnnotations(readOnlyHint=True),
    "answer_gate": ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True),
    "get_agent_call": ToolAnnotations(readOnlyHint=True),
    "cancel_run": ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True),
    "get_usage": ToolAnnotations(readOnlyHint=True),
}


class PatTokenVerifier(TokenVerifier):
    async def verify_token(self, token: str) -> AccessToken | None:
        # Auth middleware runs outside the request session boundary, so this
        # owns one — authenticate stamps last_used_at.
        try:
            pat = PersonalAccessToken.authenticate(token)
            access = AccessToken(
                token=token,
                client_id=pat.token_prefix,
                scopes=[],
                claims={"account_id": pat.account_id, "pat_id": pat.id},
            )
            db_session().commit()
            return access
        except InvalidPatError:
            db_session().rollback()
            return
        finally:
            db_session.remove()


class CallerPat(httpx.Auth):
    # The derivation strips authorization when replaying inbound headers;
    # the caller's PAT re-enters here, so each route runs as that account.
    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response, None]:
        try:
            incoming = get_http_request()
        except RuntimeError:
            incoming = None
        bearer = incoming.headers.get("authorization") if incoming else None
        if bearer:
            request.headers["Authorization"] = bearer
        yield request


def _annotate(route: object, component: object) -> None:
    if isinstance(component, OpenAPITool):
        component.annotations = _TOOL_ANNOTATIONS[component.name]


def create_mcp_app(api: FastAPI) -> StarletteWithLifespan:
    # Built directly rather than via from_fastapi, which owns the transport:
    # raise_app_exceptions=False makes an app crash reach the tool as the
    # app's sanitized 500, so no masking is needed and the taxonomy travels.
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api, raise_app_exceptions=False),
        base_url="http://druks",
        auth=CallerPat(),
    )
    provider = OpenAPIProvider(
        openapi_spec=api.openapi(),
        client=client,
        route_maps=[
            RouteMap(tags={"agent"}, mcp_type=MCPType.TOOL),
            RouteMap(mcp_type=MCPType.EXCLUDE),
        ],
        mcp_component_fn=_annotate,
    )
    server = FastMCP(
        name="druks",
        providers=[provider],
        instructions=_INSTRUCTIONS,
        auth=PatTokenVerifier(),
    )
    # Derivation primed app.openapi()'s cache mid-assembly; drop it.
    api.openapi_schema = None
    return server.http_app(path="/mcp", stateless_http=True, json_response=False)

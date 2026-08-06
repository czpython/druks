# The inbound /mcp endpoint ("server" stays reserved for the registry rows).
# Its tools are derived from the routes tagged "agent": the route is an
# operation's single declaration — schema, docstring, operation_id — and a
# tagged extension route joins the surface the same way.
import inspect
from collections.abc import Generator

import httpx
from fastapi import FastAPI
from fastapi.routing import APIRoute, iter_route_contexts
from fastmcp import FastMCP
from fastmcp.server.auth import AccessToken, TokenVerifier
from fastmcp.server.dependencies import get_http_request
from fastmcp.server.http import StarletteWithLifespan
from fastmcp.server.providers.openapi import MCPType, OpenAPIProvider, OpenAPITool, RouteMap
from fastmcp.utilities.openapi import HTTPRoute
from mcp.types import ToolAnnotations

from druks.accounts.exceptions import InvalidPatError
from druks.accounts.models import PersonalAccessToken
from druks.database import db_session
from druks.extensions.loader import iter_extensions
from druks.mcp.exceptions import InvalidAgentToolError

_INSTRUCTIONS = """\
Druks coordinates durable agent runs over shared work items. Start with
list_open_subjects; each workflow's run and latestAgentCall ids feed the
id-keyed tools.
get_gate returns a parked run's ask, a bounded artifact chunk, and parkedAt;
answer_gate must echo that parkedAt unchanged — it names the exact question
being answered, and a repeat answer reports already_answered. get_agent_call
returns bounded transcript and stderr tails, never full payloads. cancel_run
records its reason as the run's failure. retry_run reruns a failed run from
the step that killed it. get_usage is the caller's quota and today's spend.
There is no push channel; poll list_open_subjects at ~30s intervals while
waiting. Tool failures embed {code, message, retryable} from the HTTP
surface.
"""


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


def _validate_agent_tools(api: FastAPI) -> None:
    # The provider logs component-fn errors instead of raising, so derived tools
    # cannot refuse boot; validate the routes before derivation. Inclusion is
    # deferred (api.routes holds unresolved routers), so the contexts iterator
    # is the one view with every route's merged tags — and the loader tags each
    # extension route with its extension's name, so the tag names the owner.
    extension_names = {extension.name for extension in iter_extensions()}

    for route in iter_route_contexts(api.routes):
        if not isinstance(route.original_route, APIRoute) or "agent" not in route.tags:
            continue

        where = f"{'/'.join(sorted(route.methods or ()))} {route.path}"
        operation_id = route.operation_id
        if not operation_id:
            raise InvalidAgentToolError(where, "an explicit operation_id is required")
        if not inspect.getdoc(route.endpoint):
            raise InvalidAgentToolError(where, "a non-empty endpoint docstring is required")
        extension = next((tag for tag in route.tags if tag in extension_names), None)
        if extension and not operation_id.startswith(f"{extension}_"):
            raise InvalidAgentToolError(
                where, f"operation_id {operation_id!r} must start with {extension + '_'!r}"
            )


def _annotate(route: HTTPRoute, component: object) -> None:
    if isinstance(component, OpenAPITool):
        is_read = route.method == "GET"
        component.annotations = ToolAnnotations(
            readOnlyHint=is_read,
            destructiveHint=not is_read and route.extensions.get("x-destructive", True),
            idempotentHint=route.extensions.get("x-idempotent", False),
        )


def create_mcp_app(api: FastAPI) -> StarletteWithLifespan:
    _validate_agent_tools(api)
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

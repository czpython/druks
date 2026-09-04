# The inbound /mcp endpoint ("server" stays reserved for the registry rows).
# Its tools are derived from the routes tagged "agent": the route is an
# operation's single declaration — schema, docstring, operation_id — and a
# tagged app route joins the surface the same way.
import inspect
from collections.abc import Generator, Sequence

import httpx
from fastapi import FastAPI
from fastapi.routing import APIRoute, iter_route_contexts
from fastmcp import FastMCP
from fastmcp.server.auth import AccessToken, TokenVerifier
from fastmcp.server.dependencies import get_access_token, get_http_request
from fastmcp.server.http import StarletteWithLifespan
from fastmcp.server.providers.openapi import MCPType, OpenAPIProvider, OpenAPITool, RouteMap
from fastmcp.server.transforms import GetToolNext, Transform
from fastmcp.tools.base import Tool
from fastmcp.utilities.openapi import HTTPRoute
from fastmcp.utilities.versions import VersionSpec
from mcp.types import ToolAnnotations

from druks.accounts.exceptions import InvalidPatError
from druks.accounts.models import OperatorToken, PersonalAccessToken
from druks.apps.loader import iter_apps
from druks.database import db_session
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
        # owns one — authenticate stamps last_used_at on a PAT; a call token
        # is Redis-only and needs no commit.
        try:
            operator = await OperatorToken.lookup(token)
            if operator:
                return AccessToken(
                    token=token,
                    client_id=operator.agent_call_id,
                    scopes=[],
                    claims={
                        "account_id": operator.account_id,
                        "agent_call_id": operator.agent_call_id,
                        "run_id": operator.run_id,
                        "writes": operator.writes,
                    },
                )
            pat = await PersonalAccessToken.authenticate(token)
            access = AccessToken(
                token=token,
                client_id=pat.token_prefix,
                scopes=[],
                claims={"account_id": pat.account_id, "pat_id": pat.id, "writes": "allow"},
            )
            await db_session().commit()
            return access
        except InvalidPatError:
            await db_session().rollback()
            return
        finally:
            await db_session.remove()


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


def _writes_claim() -> str:
    token = get_access_token()
    if token:
        return token.claims.get("writes", "allow")
    return "allow"


def _is_read_tool(tool: Tool) -> bool:
    return bool(tool.annotations and tool.annotations.readOnlyHint)


class OperatorWritesFilter(Transform):
    """Propose (writes=deny) lists only GET-derived tools. Confirm and full
    keep the live catalog; mutating calls are intercepted on the HTTP hop."""

    async def list_tools(self, tools: Sequence[Tool]) -> Sequence[Tool]:
        if _writes_claim() == "deny":
            return [tool for tool in tools if _is_read_tool(tool)]
        return tools

    async def get_tool(
        self, name: str, call_next: GetToolNext, *, version: VersionSpec | None = None
    ) -> Tool | None:
        tool = await call_next(name, version=version)
        if tool and _writes_claim() == "deny" and not _is_read_tool(tool):
            return
        return tool


def _bearer_credential(header: str | None) -> str:
    if header:
        scheme, _, credential = header.partition(" ")
        if scheme.lower() == "bearer" and credential:
            return credential
    return ""


class OperatorWritesTransport(httpx.AsyncBaseTransport):
    """Mutating OpenAPI hops: deny 403s, defer stashes, allow passes through."""

    def __init__(self, inner: httpx.AsyncBaseTransport):
        self._inner = inner

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.method.upper() != "GET":
            operator = await OperatorToken.lookup(
                _bearer_credential(request.headers.get("authorization"))
            )
            if operator and operator.writes == "deny":
                return httpx.Response(
                    403,
                    json={
                        "code": "AUTONOMY_READ_ONLY",
                        "message": (
                            "This conversation's autonomy is propose; mutating tools do not run."
                        ),
                        "retryable": False,
                    },
                    request=request,
                )
            if operator and operator.writes == "defer":
                await OperatorToken.defer_write(
                    operator.run_id,
                    {
                        "method": request.method,
                        "path": request.url.path,
                        "body": request.content.decode() if request.content else "",
                        "content_type": request.headers.get("content-type") or "application/json",
                    },
                )
                return httpx.Response(
                    200,
                    json={
                        "result": "deferred",
                        "message": "Proposed. The operator must confirm before this runs.",
                    },
                    request=request,
                )
        return await self._inner.handle_async_request(request)


def _validate_agent_tools(api: FastAPI) -> None:
    # The provider logs component-fn errors instead of raising, so derived tools
    # cannot refuse boot; validate the routes before derivation. Inclusion is
    # deferred (api.routes holds unresolved routers), so the contexts iterator
    # is the one view with every route's merged tags. Validation owns only the
    # two demands the author owns — an explicit operation_id and a non-empty
    # docstring; the app prefix is the framework's to derive, not the
    # author's to repeat (see _namespace_agent_operations).
    for route in iter_route_contexts(api.routes):
        if not isinstance(route.original_route, APIRoute) or "agent" not in route.tags:
            continue

        where = f"{'/'.join(sorted(route.methods or ()))} {route.path}"
        if not route.operation_id:
            raise InvalidAgentToolError(where, "an explicit operation_id is required")
        if not inspect.getdoc(route.endpoint):
            raise InvalidAgentToolError(where, "a non-empty endpoint docstring is required")


def _namespace_agent_operations(spec: dict, app_names: set[str]) -> None:
    # Derive each app-owned agent operation's id to f"{app}_{operation_id}",
    # so the tool name the provider reads off the spec is namespaced without the
    # author repeating the prefix. The loader tags every app route with its
    # app's name, so among an agent operation's tags the one naming an
    # installed app is the owner; platform agent operations carry no such
    # tag and keep their declared ids. An already-prefixed id passes through, so
    # stable names like software_factory_start never double — and the namespace is what makes
    # the merged document's operation ids globally unique. A derived id that would
    # collide with another route's explicit id is rejected: before this derivation
    # the clash was visible in the author's code, so the framework must surface it
    # now that it owns the naming.
    existing_ids = {
        op.get("operationId")
        for ops in spec.get("paths", {}).values()
        for op in ops.values()
        if isinstance(op, dict) and op.get("operationId")
    }
    for path, operations in spec.get("paths", {}).items():
        for operation in operations.values():
            if not isinstance(operation, dict) or "agent" not in operation.get("tags", []):
                continue
            operation_id = operation.get("operationId")
            if not operation_id:
                continue
            app = next((tag for tag in operation["tags"] if tag in app_names), None)
            if app and not operation_id.startswith(f"{app}_"):
                derived = f"{app}_{operation_id}"
                if derived in existing_ids:
                    raise InvalidAgentToolError(
                        path,
                        f"derived operation id {derived!r} collides with existing "
                        "operation id; rename the conflicting route",
                    )
                operation["operationId"] = derived


def _install_agent_namespacing(api: FastAPI) -> None:
    # The tool name comes from the spec's operation id, so the namespace must
    # land on the document api.openapi() builds — not on FastAPI's cached, merged
    # route contexts, which later generation silently discards. Wrap the app's
    # own generator so the provider here and every later /openapi.json share one
    # namespaced document: each fresh build (including after the openapi_schema
    # reset below) re-derives the ids. generate() is the app's own api.openapi,
    # which already returns the cached schema when it is warm, so namespaced()
    # need not repeat that guard — and the derivation is idempotent, so running
    # it on a warm cache leaves the already-prefixed ids untouched.
    app_names = {app.name for app in iter_apps()}
    generate = api.openapi

    def namespaced() -> dict:
        spec = generate()
        _namespace_agent_operations(spec, app_names)
        return spec

    api.openapi = namespaced


def _annotate(route: HTTPRoute, component: object) -> None:
    if isinstance(component, OpenAPITool):
        is_read = route.method == "GET"
        component.annotations = ToolAnnotations(
            readOnlyHint=is_read,
            destructiveHint=not is_read and route.extensions.get("x-destructive", True),
            idempotentHint=route.extensions.get("x-idempotent", False),
        )
        # Confirm (writes=defer) intercepts mutating hops with a deferred stub,
        # not the route's 201 model. Advertising that model as outputSchema
        # makes MCP reject the stub (`identifier` required on create_ticket).
        if not is_read:
            component.output_schema = None


def create_mcp_app(api: FastAPI) -> StarletteWithLifespan:
    _validate_agent_tools(api)
    _install_agent_namespacing(api)
    # Built directly rather than via from_fastapi, which owns the transport:
    # raise_app_exceptions=False makes an app crash reach the tool as the
    # app's sanitized 500, so no masking is needed and the taxonomy travels.
    client = httpx.AsyncClient(
        transport=OperatorWritesTransport(httpx.ASGITransport(app=api, raise_app_exceptions=False)),
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
        transforms=[OperatorWritesFilter()],
        instructions=_INSTRUCTIONS,
        auth=PatTokenVerifier(),
    )
    # Derivation primed app.openapi()'s cache mid-assembly; drop it.
    api.openapi_schema = None
    return server.http_app(path="/mcp", stateless_http=True, json_response=False)

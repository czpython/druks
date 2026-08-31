import logging
from collections.abc import AsyncIterator, Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from fastmcp.utilities.lifespan import combine_lifespans
from starlette.datastructures import MutableHeaders
from starlette.routing import Route

from druks.accounts.dependencies import current_account, resolve_single_operator
from druks.accounts.exceptions import AuthConfigurationError
from druks.accounts.routes import router as auth_router
from druks.api.artifacts import router as artifacts_router
from druks.api.exceptions import AgentApiError
from druks.api.runs import router as runs_router
from druks.api.subjects import router as subjects_router
from druks.apps.loader import iter_apps, load
from druks.apps.routes import router as apps_router
from druks.browser.exceptions import BrowserApiError
from druks.browser.routes import router as browser_sessions_router
from druks.core.templates import render_page
from druks.database import (
    configure_session,
    create_async_engine_from_url,
    db_session,
    session_scope,
)
from druks.durable.engine import init_dbos, launch, shutdown
from druks.durable.exceptions import AgentCallNotFound
from druks.events.routes import router as events_router
from druks.files.routes import router as files_router
from druks.harnesses.routes import router as harness_connection_router
from druks.mcp.catalog import load_mcp_catalog
from druks.mcp.gateway import exceptions as gate_errors
from druks.mcp.gateway.routes import router as gateway_router
from druks.mcp.routes import router as mcp_router
from druks.notifications.routes import external_router as notifications_external_router
from druks.notifications.routes import router as notifications_router
from druks.redis import close_client
from druks.services.exceptions import OauthPageError, ServiceNotConnectedError
from druks.services.routes import oauth_router
from druks.services.routes import router as service_identities_router
from druks.settings import Settings, ensure_data_dirs, load_settings, setup_logging
from druks.skills.routes import router as skills_router
from druks.ui.exceptions import PageReadError
from druks.user_settings.routes import router as settings_router
from druks.webhooks import router as webhooks_router

from .routes import router as health_router


def configure_state(app: FastAPI, settings: Settings) -> None:
    ensure_data_dirs(settings)
    app.state.settings = settings
    app.state.engine = create_async_engine_from_url(settings.database_url)
    # Bind the ambient (``scoped_session``) factory to this engine so
    # request handlers can use ``db_session()`` without per-call setup.
    configure_session(app.state.engine)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Tests pre-populate ``app.state.settings`` before lifespan fires (their
    # engine is a fixture-owned connection this lifespan must not dispose).
    # Production hits this branch and reads env config.
    created_engine = None
    if not hasattr(app.state, "settings"):
        settings = load_settings()
        configure_state(app, settings)
        created_engine = app.state.engine
        # uvicorn runs this module directly and never calls setup_logging, so
        # app loggers default to WARNING-only and INFO is dropped; the web
        # process configures it here.
        setup_logging(settings)
        # The default-server catalog mounts before DBOS launches — recovered
        # workflows may deliver MCP servers immediately. A bad catalog stops
        # boot here, loudly. (The suite loads it from a conftest fixture.)
        load_mcp_catalog(settings.mcp_catalog_path)
        if settings.identity.mode == "none":
            # A drifted none-mode install (more than one operator account) must
            # refuse at boot, not per request; the per-request resolver repeats
            # the check for drift that happens while running.
            async with session_scope(app.state.engine):
                await resolve_single_operator()
        # DBOS runs embedded here: this process both serves HTTP and executes
        # durable workflows. Tests pre-populate app.state.settings and never
        # reach here — they drive DBOS through their own fixtures.
        init_dbos()
        await launch()
        # Each app converges its own runtime state (e.g. schedules) here, after
        # DBOS is live. A failing hook is logged, not fatal — one app can't wedge boot.
        for registered_app in iter_apps():
            try:
                await registered_app.on_startup()
            except Exception:
                logging.getLogger(__name__).exception(
                    "app %r on_startup failed", registered_app.name
                )

    try:
        yield
    finally:
        shutdown()
        if created_engine:
            await created_engine.dispose()
        await close_client()


async def _release_db_session() -> AsyncIterator[None]:
    """Bind a fresh DB session for the request and commit it on success, roll
    back on error — one transaction per request. Model writes ``flush()``
    without committing, so this is the commit boundary. The session is the
    request's own, never an ambient one already on this task (the test client
    runs requests on the caller's task), and the prior binding is restored
    after. The session object is lazy — a frontend response (the SPA, an
    app's dist/) that never touches the DB opens no connection, and the
    commit is a no-op."""
    previous = db_session() if db_session.registry.has() else None
    session = db_session.session_factory()
    db_session.registry.set(session)
    try:
        yield
    except BaseException:
        await session.rollback()
        raise
    else:
        await session.commit()
    finally:
        await session.close()
        if previous is not None:
            db_session.registry.set(previous)
        else:
            db_session.registry.clear()


def _mcp_lifespan(app: FastAPI) -> AbstractAsyncContextManager[Mapping[str, Any] | None]:
    # FastAPI never runs a plain route's lifespan; the endpoint's builds its
    # session manager. Late-bound: the endpoint derives from the assembled
    # app further down.
    return mcp_app.lifespan(app)


app = FastAPI(
    title="Druks",
    lifespan=combine_lifespans(lifespan, _mcp_lifespan),
    dependencies=[Depends(_release_db_session)],
)


# Exception handlers — uniform JSON envelope.
#
# All errors share the shape::
#
#     {"error": "<CODE>", "detail": <string-or-list>}
#
@app.exception_handler(HTTPException)
async def _http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": f"HTTP_{exc.status_code}", "detail": exc.detail},
        headers=getattr(exc, "headers", None),
    )


# The agent surface's one error shape. Messages are authored for the caller —
# the handler never serializes tracebacks or internals.
@app.exception_handler(AgentApiError)
async def _agent_api_error_handler(request: Request, exc: AgentApiError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"code": exc.code, "message": str(exc), "retryable": exc.retryable},
    )


# The engine reports a missing agent call in its own vocabulary; here it
# becomes the agent surface's wire error, serialized like every other one.
@app.exception_handler(AgentCallNotFound)
async def _agent_call_not_found_handler(request: Request, exc: AgentCallNotFound) -> JSONResponse:
    return await _agent_api_error_handler(request, gate_errors.AgentCallNotFound(exc.agent_call_id))


@app.exception_handler(PageReadError)
async def _page_read_error_handler(request: Request, exc: PageReadError) -> JSONResponse:
    logging.getLogger(__name__).exception("page read failed: %s", exc)
    return JSONResponse(status_code=500, content={"error": "PAGE_FAILED", "detail": str(exc)})


# Auth-mode drift (e.g. none mode grew a second operator account) is an
# operator problem, not a caller problem: log it loudly, answer 503.
@app.exception_handler(AuthConfigurationError)
async def _auth_configuration_handler(
    request: Request, exc: AuthConfigurationError
) -> JSONResponse:
    logging.getLogger(__name__).error("auth configuration failure: %s", exc)
    return JSONResponse(status_code=503, content={"error": "HTTP_503", "detail": str(exc)})


# Any route that resolves a service identity it hasn't connected — directly or
# through a workflow dispatch — raises this. Map it once so no entry point has
# to carry its own catch to turn it into a 409.
@app.exception_handler(ServiceNotConnectedError)
async def _service_not_connected_handler(
    request: Request, exc: ServiceNotConnectedError
) -> JSONResponse:
    return JSONResponse(status_code=409, content={"error": "HTTP_409", "detail": str(exc)})


# The connect and callback doors are reached by full-page browser navigation,
# so a failure renders an operator page, not the JSON envelope every fetch gets.
@app.exception_handler(OauthPageError)
async def _oauth_page_error_handler(request: Request, exc: OauthPageError) -> HTMLResponse:
    return render_page("service_oauth_error.html", message=str(exc), status_code=exc.status_code)


# Browser routes raise their typed error and let this name the status, so no
# route hand-maps one.
@app.exception_handler(BrowserApiError)
async def _browser_api_error_handler(request: Request, exc: BrowserApiError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": f"HTTP_{exc.status_code}", "detail": str(exc)},
    )


@app.exception_handler(RequestValidationError)
async def _validation_exception_handler(
    request: Request,
    exc: RequestValidationError,
) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={
            "error": "VALIDATION_ERROR",
            "detail": [
                {
                    "loc": list(err.get("loc", [])),
                    "msg": err.get("msg", ""),
                    "type": err.get("type", ""),
                }
                for err in exc.errors()
            ],
        },
    )


@app.exception_handler(Exception)
async def _unhandled_exception_handler(
    request: Request,
    exc: Exception,
) -> JSONResponse:
    # Never leak internal error text in the body — log it and return a
    # generic envelope. Operators read the traceback from the process
    # log; clients get a stable, scannable code.
    logging.getLogger(__name__).exception(
        "Unhandled exception in %s %s",
        request.method,
        request.url.path,
    )
    return JSONResponse(
        status_code=500,
        content={"error": "INTERNAL_ERROR", "detail": "Internal server error"},
    )


# Platform-core routers, mounted by hand at their own prefixes. App routers
# (core, software_factory, usage, …) are discovered and mounted under /api/<app> by load().
# /api sits behind the identity gate except the identity/connection surface and
# the health probe; /_external routes carry their own authentication. The
# boundary test pins the split. The auth and harness-connection routers mount
# ungated because each of their routes carries its own resolver (/me and the
# connection flow must answer during none/zero setup; capability management
# admits only the session identity).
_identity_gate = [Depends(current_account)]
app.include_router(health_router)
# Before the webhook catch-all ({hook_path:path}): declaration order is match order.
app.include_router(notifications_external_router)
app.include_router(webhooks_router)
app.include_router(auth_router)
app.include_router(harness_connection_router)
app.include_router(browser_sessions_router)
app.include_router(settings_router, dependencies=_identity_gate)
app.include_router(apps_router, dependencies=_identity_gate)
app.include_router(service_identities_router, dependencies=_identity_gate)
app.include_router(oauth_router, dependencies=_identity_gate)
app.include_router(skills_router, dependencies=_identity_gate)
app.include_router(mcp_router, dependencies=_identity_gate)
app.include_router(notifications_router, dependencies=_identity_gate)
app.include_router(events_router, dependencies=_identity_gate)
app.include_router(runs_router, dependencies=_identity_gate)
app.include_router(subjects_router, dependencies=_identity_gate)
app.include_router(gateway_router, dependencies=_identity_gate)
app.include_router(artifacts_router, dependencies=_identity_gate)
app.include_router(files_router, dependencies=_identity_gate)
load(app)

# Tools derive from every route tagged "agent", so the endpoint composes
# after load() — an app's tagged routes join tools/list too.
from druks.mcp.server import create_mcp_app  # noqa: E402

# A bare Route at exactly /mcp (a Mount would 307 the no-slash path); PATs
# authenticate it, so it sits outside the identity gate.
mcp_app = create_mcp_app(app)
app.router.routes.append(
    Route("/mcp", mcp_app, methods=["POST", "DELETE"], include_in_schema=False)
)


# Unknown /api/* paths return a JSON 404 across every method instead of
# falling through to the SPA index.html, which would mislead API consumers
# with a 200 OK + HTML. We need to catch GET/POST/PATCH/PUT/DELETE — a
# bare ``@app.get`` only caught GETs.
@app.api_route(
    "/api/{path:path}",
    methods=["GET", "POST", "PATCH", "PUT", "DELETE"],
    include_in_schema=False,
)
async def api_not_found(path: str) -> None:
    raise HTTPException(status_code=404, detail=f"Unknown API path: /api/{path}")


# The edge forwards /mcp/* but the endpoint owns only the exact path; an
# unowned subpath must not fall through to the SPA's index.html.
@app.api_route(
    "/mcp/{path:path}",
    methods=["GET", "POST", "PATCH", "PUT", "DELETE"],
    include_in_schema=False,
)
async def mcp_not_found(path: str) -> None:
    raise HTTPException(status_code=404, detail=f"Unknown MCP path: /mcp/{path}")


# Repo root in a checkout, /app in the backend image — both put the built SPA
# at <root>/dist (vite's outDir).
_SPA_DIST = Path(__file__).resolve().parents[3] / "dist"


def serve_spa(app: FastAPI, dist: Path = _SPA_DIST) -> None:
    """Serve the platform's own dashboard the way an app's ``dist/`` is
    served — FastAPI's low-priority frontend routes, so every API path (and every
    app frontend, registered earlier) wins, and unknown paths fall back to
    index.html for client-side routing. A bare pip install ships no SPA build;
    then the API serves JSON only."""
    if (dist / "index.html").is_file():
        app.frontend("/", directory=dist)


class SpaCacheControl:
    """Cache policy for the served frontends (the SPA, an app's dist/) —
    it lives with their server, not the edge proxy. Vite fingerprints every
    asset filename (index-<hash>.js), so an asset URL's content never changes:
    cache it forever. index.html is the un-fingerprinted entry point that
    references the current hashed assets — without an explicit ``no-cache`` the
    browser heuristically caches it and keeps loading old bundles after a
    deploy (the "I deployed but the UI didn't change" trap). ``no-cache`` =
    cache but always revalidate; the ETag makes that a cheap 304.

    Pure ASGI, not BaseHTTPMiddleware, so the SSE streams pass through
    untouched."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope["path"]
        # The SPA's /assets/* and an app's /app/<name>/assets/* — never
        # /api/*, whose responses must not inherit frontend cache policy.
        fingerprinted = path.startswith("/assets/") or (
            path.startswith("/app/") and "/assets/" in path
        )

        async def send_with_cache_policy(message: Any) -> None:
            if message["type"] == "http.response.start" and message["status"] == 200:
                headers = MutableHeaders(scope=message)
                html = headers.get("content-type", "").startswith("text/html")
                if fingerprinted:
                    headers.setdefault("Cache-Control", "public, max-age=31536000, immutable")
                elif html or path.startswith("/app/"):
                    # Like index.html, an app dist's entry.js and style.css keep
                    # their names across upgrades — the name is the shell's import
                    # contract — while their content changes: always revalidate.
                    headers.setdefault("Cache-Control", "no-cache")
            await send(message)

        await self.app(scope, receive, send_with_cache_policy)


serve_spa(app)
app.add_middleware(SpaCacheControl)

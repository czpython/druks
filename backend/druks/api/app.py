import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.datastructures import MutableHeaders

from druks.accounts.dependencies import current_account
from druks.accounts.routes import router as auth_router
from druks.api.agent import router as agent_router
from druks.api.artifacts import router as artifacts_router
from druks.api.runs import router as runs_router
from druks.database import configure_session, create_engine_from_url, db_session
from druks.durable.engine import init_dbos, launch, shutdown
from druks.events.routes import router as events_router
from druks.exceptions import AgentApiError
from druks.extensions.loader import iter_extensions, load
from druks.mcp.catalog import load_mcp_catalog
from druks.mcp.routes import router as mcp_router
from druks.notifications.routes import external_router as notifications_external_router
from druks.notifications.routes import router as notifications_router
from druks.redis import close_client
from druks.settings import Settings, ensure_data_dirs, load_settings, setup_logging
from druks.skills.routes import router as skills_router
from druks.user_settings.routes import router as settings_router
from druks.webhooks import router as webhooks_router

from .routes import router as health_router


def configure_state(app: FastAPI, settings: Settings) -> None:
    ensure_data_dirs(settings)
    app.state.settings = settings
    app.state.engine = create_engine_from_url(settings.database_url)
    # Bind the ambient (``scoped_session``) factory to this engine so
    # request handlers can use ``db_session()`` without per-call setup.
    configure_session(app.state.engine)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Tests pre-populate ``app.state.settings`` before lifespan fires (or
    # skip lifespan entirely by constructing ``TestClient(app)`` without
    # ``with``). Production hits this branch and reads env config.
    if not hasattr(app.state, "settings"):
        settings = load_settings()
        configure_state(app, settings)
        # uvicorn runs this module directly and never calls setup_logging, so
        # app loggers default to WARNING-only and INFO is dropped; the web
        # process configures it here.
        setup_logging(settings)
        # The default-server catalog mounts before DBOS launches — recovered
        # workflows may deliver MCP servers immediately. A bad catalog stops
        # boot here, loudly. (The suite loads it from a conftest fixture.)
        load_mcp_catalog(settings.mcp_catalog_path)
        # DBOS runs embedded here: this process both serves HTTP and executes
        # durable workflows. Tests pre-populate app.state.settings and never
        # reach here — they drive DBOS through their own fixtures.
        init_dbos()
        launch()
        # Each extension converges its own runtime state (e.g. schedules) here, after
        # DBOS is live. A failing hook is logged, not fatal — one extension can't wedge boot.
        for extension in iter_extensions():
            try:
                await extension.on_startup()
            except Exception:
                logging.getLogger(__name__).exception(
                    "extension %r on_startup failed", extension.name
                )

    try:
        yield
    finally:
        shutdown()
        # Release HTTP connection pools owned by long-lived API clients.
        linear = getattr(app.state, "linear", None)
        if linear:
            await linear.aclose()
        github = getattr(app.state, "github", None)
        if github:
            await github.aclose()
        engine = getattr(app.state, "engine", None)
        if engine:
            engine.dispose()
        await close_client()


async def _release_db_session() -> AsyncIterator[None]:
    """Commit the request's scoped DB session on success, roll back on error,
    then release it — one transaction per request. Model writes ``flush()``
    without committing, so this is the commit boundary. FastAPI runs this
    yield-dependency's teardown in the *same* asyncio task as the endpoint
    (the scoped_session is keyed by that task), so it acts on exactly the
    session this request opened. Frontend responses (the SPA, an extension's
    dist/) run app dependencies too, so only touch the registry when the
    request actually opened a session — never open one just to commit nothing.
    """
    try:
        yield
    except BaseException:
        if db_session.registry.has():
            db_session().rollback()
        raise
    else:
        if db_session.registry.has():
            db_session().commit()
    finally:
        db_session.remove()


app = FastAPI(title="Druks", lifespan=lifespan, dependencies=[Depends(_release_db_session)])


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


# Platform-core routers, mounted by hand at their own prefixes. Extension routers
# (core, build, usage, …) are discovered and mounted under /api/<extension> by load().
# /api sits behind the session gate except the login surface and the health
# probe; /_external routes carry their own authentication. The boundary test
# pins the split.
_session_gate = [Depends(current_account)]
app.include_router(health_router)
# Before the webhook catch-all ({hook_path:path}): declaration order is match order.
app.include_router(notifications_external_router)
app.include_router(webhooks_router)
app.include_router(auth_router)
app.include_router(settings_router, dependencies=_session_gate)
app.include_router(skills_router, dependencies=_session_gate)
app.include_router(mcp_router, dependencies=_session_gate)
app.include_router(notifications_router, dependencies=_session_gate)
app.include_router(events_router, dependencies=_session_gate)
app.include_router(runs_router, dependencies=_session_gate)
app.include_router(agent_router, dependencies=_session_gate)
app.include_router(artifacts_router, dependencies=_session_gate)
load(app)


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


# Repo root in a checkout, /app in the backend image — both put the built SPA
# at <root>/dist (vite's outDir).
_SPA_DIST = Path(__file__).resolve().parents[3] / "dist"


def serve_spa(app: FastAPI, dist: Path = _SPA_DIST) -> None:
    """Serve the platform's own dashboard the way an extension's ``dist/`` is
    served — FastAPI's low-priority frontend routes, so every API path (and every
    extension frontend, registered earlier) wins, and unknown paths fall back to
    index.html for client-side routing. A bare pip install ships no SPA build;
    then the API serves JSON only."""
    if (dist / "index.html").is_file():
        app.frontend("/", directory=dist)


class SpaCacheControl:
    """Cache policy for the served frontends (the SPA, an extension's dist/) —
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
        # The SPA's /assets/* and an extension's /app/<name>/assets/* — never
        # /api/*, whose responses must not inherit frontend cache policy.
        fingerprinted = path.startswith("/assets/") or (
            path.startswith("/app/") and "/assets/" in path
        )

        async def send_with_cache_policy(message: Any) -> None:
            if message["type"] == "http.response.start" and message["status"] == 200:
                headers = MutableHeaders(scope=message)
                if fingerprinted:
                    headers.setdefault("Cache-Control", "public, max-age=31536000, immutable")
                elif headers.get("content-type", "").startswith("text/html"):
                    headers.setdefault("Cache-Control", "no-cache")
            await send(message)

        await self.app(scope, receive, send_with_cache_policy)


serve_spa(app)
app.add_middleware(SpaCacheControl)

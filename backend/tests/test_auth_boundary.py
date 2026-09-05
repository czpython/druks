import pytest
from druks.accounts.dependencies import (
    current_account,
    current_account_or_setup,
    current_session_account,
    current_session_or_setup,
)
from druks.accounts.models import Account
from druks.api.server import app
from druks.harnesses.providers import get_providers
from fastapi.routing import APIRoute, _IncludedRouter

# Every /api path allowed to skip the identity gate; additions are deliberate.
EXEMPT_API_PATHS = {
    "/api/system/health",
    "/api/auth/me",
    "/api/providers",
    "/api/providers/{provider_id}/connection/start",
    "/api/providers/{provider_id}/connection/complete",
    "/api/{path:path}",  # the JSON-404 catch-all
}

# Capability management admits the session identity only — never a PAT.
SESSION_ONLY_API_ROUTES = {
    ("PUT", "/api/browser-sessions/{name}/state"),
    ("POST", "/api/browser-sessions/{name}/login-window"),
    ("POST", "/api/browser-sessions/{name}/login-window/save"),
    ("POST", "/api/browser-sessions/{name}/login-window/cancel"),
    ("DELETE", "/api/browser-sessions/{name}"),
    ("GET", "/api/auth/personal-tokens"),
    ("POST", "/api/auth/personal-tokens"),
    ("DELETE", "/api/auth/personal-tokens/{pat_id}"),
    ("GET", "/api/providers/catalogs"),
    ("GET", "/api/providers/directory"),
    ("GET", "/api/providers/subscriptions"),
    ("GET", "/api/providers/keys"),
    ("POST", "/api/providers/{provider_id}/key"),
    ("DELETE", "/api/providers/{provider_id}/key"),
    ("DELETE", "/api/providers/{provider_id}/connection"),
    ("PATCH", "/api/settings/apps"),
    ("POST", "/api/services/{slug}"),
    ("GET", "/api/oauth/{slug}/connect"),
    ("GET", "/api/oauth/connections"),
    ("DELETE", "/api/oauth/connections/{connection_id}"),
}

# Session-gated routes that also sit behind their router's identity gate —
# the route-level session dependency is the stricter of the two.
DUAL_GATED_API_PATHS = {
    "/api/settings/apps",
    "/api/services/{slug}",
    "/api/oauth/{slug}/connect",
    "/api/oauth/connections",
    "/api/oauth/connections/{connection_id}",
}

# Provider discovery and connection must answer during none/zero setup, before
# any account exists.
SETUP_CAPABLE_API_PATHS = {
    "/api/providers",
    "/api/providers/{provider_id}/connection/start",
    "/api/providers/{provider_id}/connection/complete",
}


def _walk(routes):
    # FastAPI 0.139 defers include_router into _IncludedRouter nodes; only
    # effective_candidates() shows the include-time dependencies (the gate).
    for route in routes:
        if isinstance(route, _IncludedRouter):
            yield from _walk(route.effective_candidates())
        elif isinstance(route, APIRoute) or isinstance(
            getattr(route, "original_route", None), APIRoute
        ):
            yield route
        elif hasattr(route, "routes"):
            yield from _walk(route.routes)


@pytest.fixture
def api_routes():
    return list(_walk(app.router.routes))


def _gated_by(route, gate) -> bool:
    return any(dependency.call is gate for dependency in route.dependant.dependencies)


def _is_session_only(route) -> bool:
    return any((method, route.path) in SESSION_ONLY_API_ROUTES for method in route.methods)


def test_every_internal_api_route_sits_behind_the_identity_gate(api_routes):
    unguarded = [
        route.path
        for route in api_routes
        if route.path.startswith("/api/")
        and route.path not in EXEMPT_API_PATHS
        and not _is_session_only(route)
        and not _gated_by(route, current_account)
    ]
    assert unguarded == []
    # The sweep only covers the stream families if they exist; pin that.
    paths = {route.path for route in api_routes}
    assert "/api/events/stream" in paths
    assert any(path.endswith("/transcripts/{call_id}/stream") for path in paths)


def test_the_exemptions_are_exactly_the_enumerated_ones(api_routes):
    # The other direction: nothing exempt or outside /api carries the gate.
    for route in api_routes:
        if route.path in EXEMPT_API_PATHS or not route.path.startswith("/api/"):
            assert not _gated_by(route, current_account), route.path
    assert any(route.path.startswith("/_external/") for route in api_routes)


def test_identity_bootstrap_is_the_only_setup_tolerant_read(api_routes):
    listed = [route for route in api_routes if route.path == "/api/auth/me"]
    assert listed
    for route in listed:
        assert _gated_by(route, current_account_or_setup), route.path
    for route in api_routes:
        if route.path != "/api/auth/me":
            assert not _gated_by(route, current_account_or_setup), route.path


def test_provider_setup_uses_only_the_session_or_setup_resolver(api_routes):
    listed = [route for route in api_routes if route.path in SETUP_CAPABLE_API_PATHS]
    assert {route.path for route in listed} == SETUP_CAPABLE_API_PATHS
    for route in listed:
        assert _gated_by(route, current_session_or_setup), route.path
        assert not _gated_by(route, current_account), route.path
    for route in api_routes:
        if route.path not in SETUP_CAPABLE_API_PATHS:
            assert not _gated_by(route, current_session_or_setup), route.path


async def test_provider_list_answers_before_an_account_exists(druks_client):
    assert not await Account.list_non_system()

    response = await druks_client.get("/api/providers")

    assert response.status_code == 200
    body = response.json()
    assert [item["id"] for item in body] == [provider.id for provider in get_providers()]
    by_id = {item["id"]: item for item in body}
    assert by_id["anthropic"]["billingOptions"] == ["api_key", "subscription"]
    assert by_id["openai"]["billingOptions"] == ["api_key", "subscription"]
    assert not await Account.list_non_system()


def test_capability_management_is_session_only(api_routes):
    listed = [route for route in api_routes if _is_session_only(route)]
    assert {
        (method, route.path)
        for route in listed
        for method in route.methods
        if (method, route.path) in SESSION_ONLY_API_ROUTES
    } == SESSION_ONLY_API_ROUTES
    for route in listed:
        assert _gated_by(route, current_session_account), route.path
        if route.path not in DUAL_GATED_API_PATHS:
            assert not _gated_by(route, current_account), route.path
    # And nothing else carries the session-only gate.
    for route in api_routes:
        if not _is_session_only(route):
            assert not _gated_by(route, current_session_account), route.path

import pytest
from druks.accounts.dependencies import (
    current_account,
    current_account_or_setup,
    current_admin_account,
    current_session_account,
    current_session_or_setup,
)
from druks.accounts.models import Account
from druks.api.server import app
from druks.apps.loader import iter_apps
from druks.harnesses.providers import get_providers
from fastapi.routing import APIRoute, _IncludedRouter

# Every /api path allowed to skip the identity gate; additions are deliberate.
EXEMPT_API_PATHS = {
    "/api/auth/me",
    "/api/providers",
    "/api/providers/{provider_id}/connection/start",
    "/api/providers/{provider_id}/connection/complete",
    "/api/providers/{provider_id}/connection/check",
    "/api/secrets/{identity_id}/{name}",  # a box's identity bearer, nothing else
    "/api/{path:path}",  # the JSON-404 catch-all
}

# Capability management admits the session identity only — never a PAT.
SESSION_ONLY_API_ROUTES = {
    ("POST", "/api/chat/conversations"),
    ("GET", "/api/chat/conversations/{conversation_id}"),
    ("PATCH", "/api/chat/conversations/{conversation_id}"),
    ("POST", "/api/chat/conversations/{conversation_id}/messages"),
    ("POST", "/api/chat/conversations/{conversation_id}/messages/{message_id}/retry"),
    ("POST", "/api/chat/conversations/{conversation_id}/cancel"),
    ("GET", "/api/chat/services/waha/sessions"),
    ("GET", "/api/chat/services/waha/sessions/{session_id}/qr"),
    ("GET", "/api/chat/services/twilio/numbers"),
    ("GET", "/api/chat/services/twilio/numbers/{number_id}/calls"),
    ("GET", "/api/chat/services/twilio/numbers/{number_id}/calls/{call_id}"),
    ("GET", "/api/chat/services/twilio/numbers/unlinked"),
    ("POST", "/api/chat/connections/{connection_id}/pairing"),
    ("DELETE", "/api/chat/connections/{connection_id}/pairing"),
    ("GET", "/api/chat/services/slack/link/{token}"),
    ("GET", "/api/auth/personal-tokens"),
    ("POST", "/api/auth/personal-tokens"),
    ("DELETE", "/api/auth/personal-tokens/{pat_id}"),
    ("GET", "/api/providers/catalogs"),
    ("GET", "/api/providers/directory"),
    ("GET", "/api/providers/subscriptions"),
    ("GET", "/api/providers/keys"),
    ("POST", "/api/providers/{provider_id}/sign-in/token"),
    ("DELETE", "/api/providers/{provider_id}/connection"),
    ("GET", "/api/oauth/{slug}/connect"),
    ("GET", "/api/oauth/callback"),
    ("GET", "/api/mcp-servers/oauth/callback"),
    ("GET", "/api/oauth/connections"),
    ("DELETE", "/api/oauth/connections/{connection_id}"),
}

# Shared settings and connections admit an admin from the browser only.
ADMIN_API_ROUTES = {
    ("PATCH", "/api/auth/accounts/{account_id}"),
    ("DELETE", "/api/browser-sessions/{name}"),
    ("POST", "/api/browser-sessions/{name}/login-window"),
    ("POST", "/api/browser-sessions/{name}/login-window/cancel"),
    ("POST", "/api/browser-sessions/{name}/login-window/save"),
    ("PUT", "/api/browser-sessions/{name}/state"),
    ("POST", "/api/chat/services/twilio/numbers"),
    ("DELETE", "/api/chat/services/twilio/numbers/{number_id}"),
    ("POST", "/api/chat/services/waha/sessions"),
    ("DELETE", "/api/chat/services/waha/sessions/{session_id}"),
    ("POST", "/api/chat/services/waha/sessions/{session_id}/relink"),
    ("POST", "/api/chat/connections/{connection_id}/one-time-code"),
    ("GET", "/api/core/services/{slug}/manifest/callback"),
    ("DELETE", "/api/mcp-servers/{name}"),
    ("DELETE", "/api/mcp-servers/{name}/grant"),
    ("DELETE", "/api/mcp-servers/{name}/headers"),
    ("DELETE", "/api/mcp-servers/{name}/scopes"),
    ("POST", "/api/mcp-servers/{name}/connect"),
    ("PUT", "/api/mcp-servers/{name}/headers"),
    ("PUT", "/api/mcp-servers/{name}/scopes"),
    ("PATCH", "/api/mcp-servers/{name}"),
    ("POST", "/api/mcp-servers"),
    ("POST", "/api/mcp-servers/directory"),
    ("DELETE", "/api/notifications/destinations/{destination_id}"),
    ("PATCH", "/api/notifications/destinations/{destination_id}"),
    ("POST", "/api/notifications/destinations"),
    ("DELETE", "/api/providers/{provider_id}/key"),
    ("POST", "/api/providers/{provider_id}/key"),
    ("DELETE", "/api/services/{slug}"),
    ("POST", "/api/services/{slug}"),
    ("GET", "/api/oauth/{slug}/install"),
    ("DELETE", "/api/skills/{collection_id}"),
    ("PATCH", "/api/skills/{collection_id}/skills/{name}"),
    ("POST", "/api/skills/{collection_id}/sync"),
    ("POST", "/api/skills"),
    ("PATCH", "/api/settings"),
    ("PATCH", "/api/settings/apps"),
}

# These writes also admit a personal token.
TOKEN_WRITE_API_ROUTES = {
    ("POST", "/api/browser/action"),
    ("POST", "/api/browser/evaluate"),
    ("POST", "/api/browser/navigate"),
    ("POST", "/api/dashboard/schedules/{kind}/run"),
    ("POST", "/api/gates/{run}/answer"),
    ("DELETE", "/api/mcp-servers/{name}/grant/me"),
    ("DELETE", "/api/mcp-servers/{name}/headers/me"),
    ("POST", "/api/mcp-servers/{name}/connect/me"),
    ("PUT", "/api/mcp-servers/{name}/headers/me"),
    ("POST", "/api/runs/{run}/cancel"),
    ("POST", "/api/runs/{run}/resume"),
    ("POST", "/api/runs/{run}/retry"),
    ("PATCH", "/api/settings/personal"),
}

# Session-gated routes that also sit behind their router's identity gate —
# the route-level session dependency is the stricter of the two.
DUAL_GATED_API_PATHS = {
    "/api/chat/conversations",
    "/api/chat/conversations/{conversation_id}",
    "/api/chat/conversations/{conversation_id}/messages",
    "/api/chat/conversations/{conversation_id}/messages/{message_id}/retry",
    "/api/chat/conversations/{conversation_id}/cancel",
    "/api/chat/services/waha/sessions",
    "/api/chat/services/waha/sessions/{session_id}/qr",
    "/api/chat/services/twilio/numbers",
    "/api/chat/services/twilio/numbers/{number_id}/calls",
    "/api/chat/services/twilio/numbers/{number_id}/calls/{call_id}",
    "/api/chat/services/twilio/numbers/unlinked",
    "/api/chat/connections/{connection_id}/pairing",
    "/api/chat/services/slack/link/{token}",
    "/api/oauth/{slug}/connect",
    "/api/oauth/callback",
    "/api/mcp-servers/oauth/callback",
    "/api/oauth/connections",
    "/api/oauth/connections/{connection_id}",
}

# Provider discovery and connection must answer during none/zero setup, before
# any account exists.
SETUP_CAPABLE_API_PATHS = {
    "/api/providers",
    "/api/providers/{provider_id}/connection/start",
    "/api/providers/{provider_id}/connection/complete",
    "/api/providers/{provider_id}/connection/check",
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
    return any(
        dependency.call is gate or any(nested.call is gate for nested in dependency.dependencies)
        for dependency in route.dependant.dependencies
    )


def _is_session_only(route) -> bool:
    return any((method, route.path) in SESSION_ONLY_API_ROUTES for method in route.methods)


def _route_keys(route) -> set[tuple[str, str]]:
    return {(method, route.path) for method in route.methods if method not in {"HEAD", "OPTIONS"}}


def test_every_internal_api_route_sits_behind_the_identity_gate(api_routes):
    unguarded = [
        route.path
        for route in api_routes
        if route.path.startswith("/api/")
        and route.path not in EXEMPT_API_PATHS
        and not _is_session_only(route)
        and not any(key in ADMIN_API_ROUTES for key in _route_keys(route))
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


async def test_provider_list_answers_before_an_account_exists(druks_db, druks_client):
    assert not await Account.list_operators(druks_db)

    response = await druks_client.get("/api/providers")

    assert response.status_code == 200
    body = response.json()
    assert [item["id"] for item in body] == [provider.id for provider in get_providers()]
    by_id = {item["id"]: item for item in body}
    assert by_id["anthropic"]["billingOptions"] == ["api_key", "subscription"]
    assert by_id["openai"]["billingOptions"] == ["api_key", "subscription"]
    assert not await Account.list_operators(druks_db)


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
        if not _is_session_only(route) and not any(
            key in ADMIN_API_ROUTES for key in _route_keys(route)
        ):
            assert not _gated_by(route, current_session_account), route.path


def test_admin_routes_use_only_the_admin_gate(api_routes):
    listed = [
        route for route in api_routes if any(key in ADMIN_API_ROUTES for key in _route_keys(route))
    ]
    assert {key for route in listed for key in _route_keys(route) if key in ADMIN_API_ROUTES} == (
        ADMIN_API_ROUTES
    )
    for route in listed:
        assert _gated_by(route, current_admin_account), route.path
        assert not {"agent", "bot"} & set(route.tags), route.path
    for route in api_routes:
        if not any(key in ADMIN_API_ROUTES for key in _route_keys(route)):
            assert not _gated_by(route, current_admin_account), route.path


def test_every_platform_write_has_one_access_class(api_routes):
    known_groups = (ADMIN_API_ROUTES, SESSION_ONLY_API_ROUTES, TOKEN_WRITE_API_ROUTES)
    assert not any(
        left & right
        for index, left in enumerate(known_groups)
        for right in known_groups[index + 1 :]
    )
    known_route_keys = {key for route in api_routes for key in _route_keys(route)}
    assert set().union(*known_groups) <= known_route_keys

    app_prefixes = tuple(f"/api/{registered_app.name}/" for registered_app in iter_apps())
    for route in api_routes:
        if not route.path.startswith("/api/") or route.path.startswith(app_prefixes):
            continue
        for key in _route_keys(route):
            if key[0] == "GET":
                continue
            groups = [group for group in known_groups if key in group]
            if route.path in SETUP_CAPABLE_API_PATHS:
                groups.append(SETUP_CAPABLE_API_PATHS)
            elif route.path in EXEMPT_API_PATHS:
                groups.append(EXEMPT_API_PATHS)
            assert len(groups) == 1, key

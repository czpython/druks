import asyncio
import base64
import hashlib
import json
from urllib.parse import parse_qsl, urlparse

import httpx
import pytest
from conftest import configure_app_for_test, make_settings
from druks.extensions.registry import mcp_servers
from druks.mcp import oauth
from druks.mcp.constants import (
    OAUTH_ACCESS_TOKEN_PREFIX,
    OAUTH_CONNECT_STATE_PREFIX,
    OAUTH_REFRESH_LOCK_PREFIX,
)
from druks.mcp.enums import TokenSource
from druks.mcp.exceptions import GrantRefreshError, MissingGrantError, OauthConnectError
from druks.mcp.helpers import get_bearer_token_env_var
from druks.mcp.models import McpOauthGrant, McpServer
from druks.redis import get_client
from druks.sandbox.datastructures import Workspace
from fastapi.testclient import TestClient

_NAME = "linear_oauth"
_SERVER_URL = "https://mcp.linear.test/sse"
_AUTH_BASE = "https://auth.linear.test"
_ENDPOINT = "https://druks.example"
_CALLBACK = f"{_ENDPOINT}/api/mcp-servers/oauth/callback"


class _FakeSandbox:
    ssh_username = "exedev"


class FakeAuthServer:
    # The RFC 9728 / 8414 / 7591 surface the connect flow talks to, plus a
    # token endpoint that records every request it answers.
    def __init__(self) -> None:
        self.discovery_supported = True
        self.registration_supported = True
        self.resource = _SERVER_URL
        self.issuer = _AUTH_BASE
        self.code_challenge_methods = ["S256"]
        self.token_status = 200
        self.token_malformed = False
        self.token_response = {
            "access_token": "at-1",
            "refresh_token": "rt-1",
            "expires_in": 3600,
            "scope": "read",
        }
        self.token_requests: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.startswith("/.well-known/oauth-protected-resource"):
            if not self.discovery_supported:
                return httpx.Response(404)
            return httpx.Response(
                200, json={"resource": self.resource, "authorization_servers": [_AUTH_BASE]}
            )
        if path.startswith("/.well-known/oauth-authorization-server") or path.startswith(
            "/.well-known/openid-configuration"
        ):
            if not self.discovery_supported:
                return httpx.Response(404)
            metadata = {
                "issuer": self.issuer,
                "authorization_endpoint": f"{_AUTH_BASE}/authorize",
                "token_endpoint": f"{_AUTH_BASE}/token",
                "code_challenge_methods_supported": self.code_challenge_methods,
            }
            if self.registration_supported:
                metadata["registration_endpoint"] = f"{_AUTH_BASE}/register"
            return httpx.Response(200, json=metadata)
        if path == "/register":
            return httpx.Response(201, json={"client_id": "client-123"})
        if path == "/token":
            self.token_requests.append(dict(parse_qsl(request.content.decode())))
            if self.token_malformed:
                return httpx.Response(200, content=b"not json")
            return httpx.Response(self.token_status, json=self.token_response)
        return httpx.Response(404)


@pytest.fixture
def auth_server(monkeypatch):
    fake = FakeAuthServer()
    monkeypatch.setattr(
        oauth, "_http", lambda: httpx.AsyncClient(transport=httpx.MockTransport(fake.handler))
    )
    return fake


@pytest.fixture(autouse=True)
async def _clean_oauth_redis():
    # The suite shares one FakeRedis; OAuth keys are server-name-keyed, so a
    # cached token from one test would satisfy the next test's mint.
    redis = get_client()
    for key in list(redis._data):
        if key.startswith("mcp:oauth:"):
            await redis.delete(key)
    yield


def _register_oauth_server(name: str = _NAME, enabled: bool = True) -> None:
    mcp_servers.register(
        {
            "name": name,
            "url": _SERVER_URL,
            "token_source": TokenSource.OAUTH,
            "source_env_var": "",
            "enabled": enabled,
        }
    )


def _store_grant(refresh_token: str = "rt-1") -> McpOauthGrant:
    return McpOauthGrant.store(
        server_name=_NAME,
        refresh_token=refresh_token,
        token_endpoint=f"{_AUTH_BASE}/token",
        resource=_SERVER_URL,
        client_id="client-123",
    )


# --- connect: discovery + DCR + PKCE ---------------------------------------


async def test_begin_connect_builds_consent_url_and_stashes_pkce_state(auth_server):
    url = await oauth.begin_connect(_NAME, _SERVER_URL, _ENDPOINT)

    assert url.startswith(f"{_AUTH_BASE}/authorize?")
    params = dict(parse_qsl(urlparse(url).query))
    assert params["response_type"] == "code"
    assert params["client_id"] == "client-123"
    assert params["redirect_uri"] == _CALLBACK
    assert params["code_challenge_method"] == "S256"
    assert params["resource"] == _SERVER_URL

    raw = await get_client().get(f"{OAUTH_CONNECT_STATE_PREFIX}{params['state']}")
    pending = json.loads(raw)
    # The challenge in the consent URL is the S256 hash of the stashed verifier.
    expected = (
        base64.urlsafe_b64encode(hashlib.sha256(pending["code_verifier"].encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    assert params["code_challenge"] == expected
    assert pending["token_endpoint"] == f"{_AUTH_BASE}/token"
    assert pending["name"] == _NAME


async def test_begin_connect_without_registration_endpoint_fails_loudly(auth_server):
    auth_server.registration_supported = False

    with pytest.raises(OauthConnectError, match="dynamic client registration"):
        await oauth.begin_connect(_NAME, _SERVER_URL, _ENDPOINT)


async def test_begin_connect_without_metadata_fails_loudly(auth_server):
    auth_server.discovery_supported = False

    with pytest.raises(OauthConnectError, match="no authorization-server metadata"):
        await oauth.begin_connect(_NAME, _SERVER_URL, _ENDPOINT)


async def test_begin_connect_rejects_resource_metadata_about_another_server(auth_server):
    # RFC 9728: the protected-resource document must be about the server we
    # asked after — a mismatch means we'd bind a grant to the wrong audience.
    auth_server.resource = "https://other.test/sse"

    with pytest.raises(OauthConnectError, match="protected-resource metadata"):
        await oauth.begin_connect(_NAME, _SERVER_URL, _ENDPOINT)


async def test_begin_connect_skips_metadata_claiming_another_issuer(auth_server):
    # RFC 8414: metadata claiming a different issuer is not ours (the mix-up
    # defense) — skipped, and with no candidate left the flow fails loudly.
    auth_server.issuer = "https://evil.test"

    with pytest.raises(OauthConnectError, match="no authorization-server metadata"):
        await oauth.begin_connect(_NAME, _SERVER_URL, _ENDPOINT)


async def test_begin_connect_requires_s256_when_methods_are_advertised(auth_server):
    auth_server.code_challenge_methods = ["plain"]

    with pytest.raises(OauthConnectError, match="S256"):
        await oauth.begin_connect(_NAME, _SERVER_URL, _ENDPOINT)


async def test_complete_connect_exchanges_code_and_stores_the_grant(auth_server, db_session):
    url = await oauth.begin_connect(_NAME, _SERVER_URL, _ENDPOINT)
    state = dict(parse_qsl(urlparse(url).query))["state"]

    name = await oauth.complete_connect(state=state, code="code-1")

    assert name == _NAME
    grant = McpOauthGrant.get_for_server(_NAME)
    assert grant.refresh_token.decrypt() == "rt-1"
    assert grant.resource == _SERVER_URL
    assert grant.client_id == "client-123"
    exchange = auth_server.token_requests[0]
    assert exchange["grant_type"] == "authorization_code"
    assert exchange["code"] == "code-1"
    assert exchange["code_verifier"]
    assert exchange["resource"] == _SERVER_URL

    # Nothing is cached at connect (the grant is real only once this commits);
    # the first delivery mints from it, carrying the grant's resource binding.
    assert not await get_client().get(f"{OAUTH_ACCESS_TOKEN_PREFIX}{_NAME}")
    assert await oauth.mint_access_token(_NAME) == "at-1"
    refresh = auth_server.token_requests[1]
    assert refresh["grant_type"] == "refresh_token"
    assert refresh["resource"] == _SERVER_URL

    # The state is single-use.
    with pytest.raises(OauthConnectError, match="expired state"):
        await oauth.complete_connect(state=state, code="code-1")


async def test_complete_connect_without_refresh_token_stores_nothing(auth_server, db_session):
    auth_server.token_response = {"access_token": "at-1", "expires_in": 3600}
    url = await oauth.begin_connect(_NAME, _SERVER_URL, _ENDPOINT)
    state = dict(parse_qsl(urlparse(url).query))["state"]

    with pytest.raises(OauthConnectError, match="no refresh token"):
        await oauth.complete_connect(state=state, code="code-1")
    assert not McpOauthGrant.get_for_server(_NAME)


# --- mint: cache, refresh, rotation, eviction -------------------------------


async def test_mint_refreshes_on_cache_miss_and_persists_rotation(auth_server, db_session):
    _store_grant(refresh_token="rt-old")
    auth_server.token_response = {
        "access_token": "at-2",
        "refresh_token": "rt-new",
        "expires_in": 300,
    }

    token = await oauth.mint_access_token(_NAME)

    assert token == "at-2"
    refresh = auth_server.token_requests[0]
    assert refresh["grant_type"] == "refresh_token"
    # The wire request carries the plaintext; the row carried only ciphertext.
    assert refresh["refresh_token"] == "rt-old"
    assert refresh["resource"] == _SERVER_URL
    # Rotation: the provider's new refresh token replaced the stored one.
    assert McpOauthGrant.get_for_server(_NAME).refresh_token.decrypt() == "rt-new"

    # A second mint within the TTL reuses the cache — no second refresh.
    assert await oauth.mint_access_token(_NAME) == "at-2"
    assert len(auth_server.token_requests) == 1


async def test_mint_without_grant_fails_loudly(db_session):
    with pytest.raises(MissingGrantError, match=_NAME):
        await oauth.mint_access_token(_NAME)


async def test_mint_refresh_rejection_fails_loudly_and_evicts_the_cache(auth_server, db_session):
    _store_grant()
    auth_server.token_status = 400

    with pytest.raises(GrantRefreshError, match=_NAME):
        await oauth.mint_access_token(_NAME)
    assert not await get_client().get(f"{OAUTH_ACCESS_TOKEN_PREFIX}{_NAME}")


async def test_mint_rejects_a_malformed_token_response(auth_server, db_session):
    _store_grant()
    auth_server.token_malformed = True

    with pytest.raises(GrantRefreshError, match="malformed JSON"):
        await oauth.mint_access_token(_NAME)


async def test_mint_rejects_a_token_response_without_an_access_token(auth_server, db_session):
    _store_grant()
    auth_server.token_response = {"refresh_token": "rt-2", "expires_in": 3600}

    with pytest.raises(GrantRefreshError, match="no access token"):
        await oauth.mint_access_token(_NAME)


async def test_mint_losing_the_refresh_lock_polls_for_the_winners_token(
    auth_server, db_session, monkeypatch
):
    # A second minter never refreshes (one rotation spender per server) and
    # never blocks the event loop: it polls until the winner's token appears
    # in the cache. The winner here is a concurrent task holding the lock.
    _store_grant()
    redis = get_client()
    monkeypatch.setattr(oauth, "OAUTH_MINT_WAIT_INTERVAL_SECONDS", 0)
    await redis.set(f"{OAUTH_REFRESH_LOCK_PREFIX}{_NAME}", "1")

    async def _winner_finishes():
        await redis.set(f"{OAUTH_ACCESS_TOKEN_PREFIX}{_NAME}", "at-winner")
        await redis.delete(f"{OAUTH_REFRESH_LOCK_PREFIX}{_NAME}")

    winner = asyncio.create_task(_winner_finishes())
    assert await oauth.mint_access_token(_NAME) == "at-winner"
    await winner
    assert not auth_server.token_requests


async def test_mint_times_out_loudly_when_the_refresh_lock_never_frees(db_session, monkeypatch):
    _store_grant()
    monkeypatch.setattr(oauth, "OAUTH_MINT_WAIT_INTERVAL_SECONDS", 0)
    monkeypatch.setattr(oauth, "OAUTH_MINT_WAIT_ATTEMPTS", 3)
    await get_client().set(f"{OAUTH_REFRESH_LOCK_PREFIX}{_NAME}", "1")

    with pytest.raises(GrantRefreshError, match="concurrent refresh"):
        await oauth.mint_access_token(_NAME)


# --- delivery: the oauth branch of the fold ---------------------------------


async def test_delivery_mints_and_injects_the_oauth_token(registry_state, auth_server, db_session):
    _register_oauth_server()
    _store_grant()

    kwargs = await Workspace(sandbox=_FakeSandbox()).with_mcp_servers()  # type: ignore[arg-type]

    var = get_bearer_token_env_var(_NAME)
    assert kwargs["extra_env"][var] == "at-1"
    entry = next(s for s in kwargs["mcp_servers"] if s.name == _NAME)
    assert entry.url == _SERVER_URL
    assert entry.bearer_token_env_var == var
    assert "at-1" not in repr(entry)


async def test_delivery_fails_loudly_for_an_unconnected_enabled_oauth_server(
    registry_state, db_session
):
    _register_oauth_server()

    with pytest.raises(MissingGrantError, match=_NAME):
        await Workspace(sandbox=_FakeSandbox()).with_mcp_servers()  # type: ignore[arg-type]


# --- API: connect / callback / disconnect / badge ---------------------------


def test_connect_route_requires_druks_endpoint(tmp_path, registry_state, db_session):
    _register_oauth_server()
    with TestClient(configure_app_for_test(settings=make_settings(tmp_path))) as client:
        response = client.post(f"/api/mcp-servers/{_NAME}/connect")
        assert response.status_code == 409
        assert "DRUKS_ENDPOINT" in response.text


def test_connect_route_rejects_a_non_oauth_server(tmp_path, db_session):
    with TestClient(configure_app_for_test(settings=make_settings(tmp_path))) as client:
        assert client.post("/api/mcp-servers/github/connect").status_code == 404


def test_connect_route_returns_the_consent_url(tmp_path, registry_state, auth_server, db_session):
    _register_oauth_server()
    settings = make_settings(tmp_path, endpoint=_ENDPOINT)
    with TestClient(configure_app_for_test(settings=settings)) as client:
        response = client.post(f"/api/mcp-servers/{_NAME}/connect")
        assert response.status_code == 200
        assert response.json()["authorizationUrl"].startswith(f"{_AUTH_BASE}/authorize?")


async def test_callback_route_completes_the_connect(
    tmp_path, registry_state, auth_server, db_session
):
    _register_oauth_server(enabled=False)
    url = await oauth.begin_connect(_NAME, _SERVER_URL, _ENDPOINT)
    state = dict(parse_qsl(urlparse(url).query))["state"]

    with TestClient(configure_app_for_test(settings=make_settings(tmp_path))) as client:
        page = client.get("/api/mcp-servers/oauth/callback", params={"state": state, "code": "c"})
        assert page.status_code == 200
        assert "Connected" in page.text
        # The page notifies the opener tab, then closes itself.
        assert "BroadcastChannel('druks-mcp-connect')" in page.text
        assert "window.close()" in page.text
        assert McpOauthGrant.get_for_server(_NAME)
        # Connecting is the explicit "use this server" — it enables too.
        assert McpServer.get_for_name(_NAME).is_enabled is True

        # Consent denied / unknown state both land loudly, storing nothing.
        assert (
            client.get(
                "/api/mcp-servers/oauth/callback",
                params={"state": "x", "code": "c", "error": "access_denied"},
            ).status_code
            == 400
        )
        assert (
            client.get(
                "/api/mcp-servers/oauth/callback", params={"state": "gone", "code": "c"}
            ).status_code
            == 400
        )


async def test_disconnect_route_drops_grant_and_cache(
    tmp_path, registry_state, auth_server, db_session
):
    _register_oauth_server()
    _store_grant()
    await oauth.mint_access_token(_NAME)

    with TestClient(configure_app_for_test(settings=make_settings(tmp_path))) as client:
        assert client.delete(f"/api/mcp-servers/{_NAME}/grant").status_code == 204
        assert not McpOauthGrant.get_for_server(_NAME)
        assert not await get_client().get(f"{OAUTH_ACCESS_TOKEN_PREFIX}{_NAME}")
        # The mirror of connect-enables: no grant, no calls, so no dead entry
        # riding into VMs.
        assert McpServer.get_for_name(_NAME).is_enabled is False
        assert client.delete(f"/api/mcp-servers/{_NAME}/grant").status_code == 404


def test_api_has_token_reflects_the_grant_and_leaks_no_secret(tmp_path, registry_state, db_session):
    _register_oauth_server()
    with TestClient(configure_app_for_test(settings=make_settings(tmp_path))) as client:
        server = next(s for s in client.get("/api/mcp-servers").json() if s["name"] == _NAME)
        assert server["tokenSource"] == "oauth"
        assert server["hasToken"] is False

        _store_grant(refresh_token="rt-secret-value")
        listed = client.get("/api/mcp-servers")
        server = next(s for s in listed.json() if s["name"] == _NAME)
        assert server["hasToken"] is True
        assert "rt-secret-value" not in listed.text

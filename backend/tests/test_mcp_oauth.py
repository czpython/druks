import asyncio
import base64
import hashlib
import json
from urllib.parse import parse_qsl, urlparse

import druks.redis
import httpx
import pytest
from druks.accounts.constants import SYSTEM_ACCOUNT_ID
from druks.accounts.models import Account
from druks.apps.registry import mcp_servers
from druks.mcp import oauth
from druks.mcp.enums import IdentityMode, TokenSource
from druks.mcp.exceptions import (
    GrantRefreshError,
    MissingGrantError,
    OauthConnectError,
    UnresolvedGrantAccountError,
)
from druks.mcp.helpers import get_bearer_token_env_var, get_grant_account
from druks.mcp.models import McpClientRegistration, McpServer
from druks.redis import close_client, get_client
from druks.services.models import OauthConnection
from druks.testing import configure_app_for_test, make_settings
from druks.user_settings.models import UserSettings
from druks.workspaces import Workspace
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
        self.resource_metadata_supported = True
        self.registration_supported = True
        self.resource = _SERVER_URL
        self.issuer = _AUTH_BASE
        self.code_challenge_methods = ["S256"]
        self.userinfo_endpoint = f"{_AUTH_BASE}/userinfo"
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
            if not (self.discovery_supported and self.resource_metadata_supported):
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
                "userinfo_endpoint": self.userinfo_endpoint,
                "code_challenge_methods_supported": self.code_challenge_methods,
            }
            if self.registration_supported:
                metadata["registration_endpoint"] = f"{_AUTH_BASE}/register"
            return httpx.Response(200, json=metadata)
        if path == "/userinfo":
            return httpx.Response(200, json={"email": "op@linear.test"})
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

    def http():
        return httpx.AsyncClient(transport=httpx.MockTransport(fake.handler))

    monkeypatch.setattr(oauth, "_http", http)
    monkeypatch.setattr("druks.services.oauth._http", http)
    return fake


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


async def _store_grant(
    refresh_token: str = "rt-1",
    *,
    account_id: str = SYSTEM_ACCOUNT_ID,
    identity_mode: IdentityMode = IdentityMode.SHARED,
) -> OauthConnection:
    server = await McpServer.get_for_name(_NAME)
    if not server:
        server = await McpServer.create(
            name=_NAME,
            url=_SERVER_URL,
            token_source=TokenSource.OAUTH,
        )
    server.identity_mode = identity_mode
    await McpClientRegistration.store(
        server_id=server.id,
        account_id=account_id,
        token_endpoint=f"{_AUTH_BASE}/token",
        client_id="client-123",
    )
    return await OauthConnection.create(
        provider=f"mcp:{_NAME}",
        account_id=account_id,
        refresh_token=refresh_token,
        scopes=[],
    )


def _state_key(state: str) -> str:
    return f"oauth:connect:{state}"


async def _token_key(account_id: str) -> str:
    return f"mcp:{_NAME}:access_token:{(await oauth.get_connection(_NAME, account_id)).id}"


async def _lock_key(account_id: str) -> str:
    return f"mcp:{_NAME}:refresh_lock:{(await oauth.get_connection(_NAME, account_id)).id}"


@pytest.mark.parametrize(
    ("identity_mode", "account_id"),
    (
        (None, "account-1"),
        ("unknown", "account-1"),
        (IdentityMode.PER_USER, None),
    ),
)
def test_get_grant_account_rejects_unresolved_modes(identity_mode, account_id):
    with pytest.raises(UnresolvedGrantAccountError):
        get_grant_account(identity_mode, account_id)


# --- connect: discovery + DCR + PKCE ---------------------------------------


async def test_begin_connect_builds_consent_url_and_stashes_pkce_state(auth_server):
    url = await oauth.begin_connect(
        _NAME,
        _SERVER_URL,
        _ENDPOINT,
        account_id=SYSTEM_ACCOUNT_ID,
        identity_mode=IdentityMode.SHARED,
    )

    assert url.startswith(f"{_AUTH_BASE}/authorize?")
    params = dict(parse_qsl(urlparse(url).query))
    assert params["response_type"] == "code"
    assert params["client_id"] == "client-123"
    assert params["redirect_uri"] == _CALLBACK
    assert params["code_challenge_method"] == "S256"
    assert params["resource"] == _SERVER_URL

    raw = await get_client().get(_state_key(params["state"]))
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
    assert pending["account_id"] == SYSTEM_ACCOUNT_ID
    assert pending["identity_mode"] == IdentityMode.SHARED


async def test_begin_connect_without_registration_endpoint_fails_loudly(auth_server):
    auth_server.registration_supported = False

    with pytest.raises(OauthConnectError, match="dynamic client registration"):
        await oauth.begin_connect(
            _NAME,
            _SERVER_URL,
            _ENDPOINT,
            account_id=SYSTEM_ACCOUNT_ID,
            identity_mode=IdentityMode.SHARED,
        )


async def test_begin_connect_without_metadata_fails_loudly(auth_server):
    auth_server.discovery_supported = False

    with pytest.raises(OauthConnectError, match="no authorization-server metadata"):
        await oauth.begin_connect(
            _NAME,
            _SERVER_URL,
            _ENDPOINT,
            account_id=SYSTEM_ACCOUNT_ID,
            identity_mode=IdentityMode.SHARED,
        )


async def test_begin_connect_rejects_resource_metadata_about_another_server(auth_server):
    # RFC 9728: the protected-resource document must be about the server we
    # asked after — a mismatch means we'd bind a grant to the wrong audience.
    auth_server.resource = "https://other.test/sse"

    with pytest.raises(OauthConnectError, match="protected-resource metadata"):
        await oauth.begin_connect(
            _NAME,
            _SERVER_URL,
            _ENDPOINT,
            account_id=SYSTEM_ACCOUNT_ID,
            identity_mode=IdentityMode.SHARED,
        )


async def test_begin_connect_skips_metadata_claiming_another_issuer(auth_server):
    # RFC 8414: metadata claiming a different issuer is not ours (the mix-up
    # defense) — skipped, and with no candidate left the flow fails loudly.
    auth_server.issuer = "https://evil.test"

    with pytest.raises(OauthConnectError, match="no authorization-server metadata"):
        await oauth.begin_connect(
            _NAME,
            _SERVER_URL,
            _ENDPOINT,
            account_id=SYSTEM_ACCOUNT_ID,
            identity_mode=IdentityMode.SHARED,
        )


async def test_begin_connect_follows_the_origin_referral_to_an_alias_issuer(auth_server):
    # Atlassian's shape: no protected-resource metadata, and the AS document the
    # resource origin serves claims a CDN-alias issuer. The origin's claim is the
    # same assertion RFC 9728 makes with authorization_servers, so it is followed
    # once — and the referred document must claim itself, which here it does (the
    # fake serves the same metadata on every host). The mix-up defense holds:
    # with protected-resource metadata present, the foreign claim stays rejected
    # (see the test above).
    auth_server.resource_metadata_supported = False
    auth_server.issuer = "https://alias.test"

    url = await oauth.begin_connect(
        _NAME,
        _SERVER_URL,
        _ENDPOINT,
        account_id=SYSTEM_ACCOUNT_ID,
        identity_mode=IdentityMode.SHARED,
    )

    assert url.startswith(f"{_AUTH_BASE}/authorize")


async def test_begin_connect_requires_s256_when_methods_are_advertised(auth_server):
    auth_server.code_challenge_methods = ["plain"]

    with pytest.raises(OauthConnectError, match="S256"):
        await oauth.begin_connect(
            _NAME,
            _SERVER_URL,
            _ENDPOINT,
            account_id=SYSTEM_ACCOUNT_ID,
            identity_mode=IdentityMode.SHARED,
        )


async def test_complete_connect_exchanges_code_and_stores_the_grant(auth_server, druks_db):
    url = await oauth.begin_connect(
        _NAME,
        _SERVER_URL,
        _ENDPOINT,
        account_id=SYSTEM_ACCOUNT_ID,
        identity_mode=IdentityMode.SHARED,
    )
    state = dict(parse_qsl(urlparse(url).query))["state"]

    name = await oauth.complete_connect(state=state, code="code-1")

    assert name == _NAME
    grant = await oauth.get_connection(_NAME, SYSTEM_ACCOUNT_ID)
    assert grant.refresh_token.decrypt() == "rt-1"
    assert grant.identity == {"email": "op@linear.test"}
    registration = await McpClientRegistration.get_for_account(_NAME, SYSTEM_ACCOUNT_ID)
    assert registration.client_id == "client-123"
    assert registration.token_endpoint == f"{_AUTH_BASE}/token"
    exchange = auth_server.token_requests[0]
    assert exchange["grant_type"] == "authorization_code"
    assert exchange["code"] == "code-1"
    assert exchange["code_verifier"]
    assert exchange["resource"] == _SERVER_URL

    # Nothing is cached at connect (the grant is real only once this commits);
    # the first delivery mints from it, carrying the grant's resource binding.
    assert not await get_client().get(await _token_key(SYSTEM_ACCOUNT_ID))
    assert await oauth.get_access_token(_NAME, SYSTEM_ACCOUNT_ID) == "at-1"
    refresh = auth_server.token_requests[1]
    assert refresh["grant_type"] == "refresh_token"
    assert refresh["resource"] == _SERVER_URL

    # The state is single-use.
    with pytest.raises(OauthConnectError, match="expired state"):
        await oauth.complete_connect(state=state, code="code-1")


async def test_off_issuer_userinfo_is_dropped_and_logged(auth_server, druks_db, caplog):
    auth_server.userinfo_endpoint = "https://evil.test/collect"
    url = await oauth.begin_connect(
        _NAME,
        _SERVER_URL,
        _ENDPOINT,
        account_id=SYSTEM_ACCOUNT_ID,
        identity_mode=IdentityMode.SHARED,
    )
    state = dict(parse_qsl(urlparse(url).query))["state"]

    with caplog.at_level("WARNING"):
        await oauth.complete_connect(state=state, code="code-1")

    assert (await oauth.get_connection(_NAME, SYSTEM_ACCOUNT_ID)).identity == {}
    assert "evil.test" in caplog.text


async def test_complete_connect_without_refresh_token_stores_nothing(auth_server, druks_db):
    auth_server.token_response = {"access_token": "at-1", "expires_in": 3600}
    url = await oauth.begin_connect(
        _NAME,
        _SERVER_URL,
        _ENDPOINT,
        account_id=SYSTEM_ACCOUNT_ID,
        identity_mode=IdentityMode.SHARED,
    )
    state = dict(parse_qsl(urlparse(url).query))["state"]

    with pytest.raises(OauthConnectError, match="no refresh token"):
        await oauth.complete_connect(state=state, code="code-1")
    assert not await oauth.list_connections(_NAME)


async def test_reconsent_replaces_the_grant_and_evicts_the_stale_token(auth_server, druks_db):
    await _store_grant(refresh_token="rt-stale")
    await get_client().set(await _token_key(SYSTEM_ACCOUNT_ID), "at-stale")

    url = await oauth.begin_connect(
        _NAME,
        _SERVER_URL,
        _ENDPOINT,
        account_id=SYSTEM_ACCOUNT_ID,
        identity_mode=IdentityMode.SHARED,
    )
    state = dict(parse_qsl(urlparse(url).query))["state"]
    await oauth.complete_connect(state=state, code="code-1")

    grant = await oauth.get_connection(_NAME, SYSTEM_ACCOUNT_ID)
    assert grant.refresh_token.decrypt() == "rt-1"
    # The stale narrow token must not keep serving until its TTL runs out.
    assert not await get_client().get(await _token_key(SYSTEM_ACCOUNT_ID))


async def test_reconnect_after_disconnect_creates_a_new_grant(auth_server, druks_db):
    await _store_grant(refresh_token="rt-stale")
    revoked = await oauth.get_connection(_NAME, SYSTEM_ACCOUNT_ID)
    await oauth.disconnect(_NAME, SYSTEM_ACCOUNT_ID)
    assert not await oauth.get_connection(_NAME, SYSTEM_ACCOUNT_ID)

    url = await oauth.begin_connect(
        _NAME,
        _SERVER_URL,
        _ENDPOINT,
        account_id=SYSTEM_ACCOUNT_ID,
        identity_mode=IdentityMode.SHARED,
    )
    state = dict(parse_qsl(urlparse(url).query))["state"]
    await oauth.complete_connect(state=state, code="code-1")

    from druks.database import db_session

    db_session().expunge_all()
    # A re-connect creates a new grant. The revoked row stays as history,
    # so at most one live connection holds the (server, account) slot.
    grant = await oauth.get_connection(_NAME, SYSTEM_ACCOUNT_ID)
    assert grant.id != revoked.id
    assert grant.refresh_token.decrypt() == "rt-1"
    assert revoked.revoked_at


async def test_two_shared_connects_converge_on_one_grant(auth_server, druks_db):
    first = await Account.get_or_create("first@example.com")
    second = await Account.get_or_create("second@example.com")

    for account in (first, second):
        url = await oauth.begin_connect(
            _NAME,
            _SERVER_URL,
            _ENDPOINT,
            account_id=account.id,
            identity_mode=IdentityMode.SHARED,
        )
        state = dict(parse_qsl(urlparse(url).query))["state"]
        await oauth.complete_connect(state=state, code=account.id)

    grants = await oauth.list_connections(_NAME)
    assert (await McpServer.get_for_name(_NAME)).identity_mode == IdentityMode.SHARED
    assert [grant.account_id for grant in grants] == [SYSTEM_ACCOUNT_ID]


async def test_two_per_user_connects_store_two_grants(auth_server, druks_db):
    first = await Account.get_or_create("first@example.com")
    second = await Account.get_or_create("second@example.com")

    for account in (first, second):
        url = await oauth.begin_connect(
            _NAME,
            _SERVER_URL,
            _ENDPOINT,
            account_id=account.id,
            identity_mode=IdentityMode.PER_USER,
        )
        state = dict(parse_qsl(urlparse(url).query))["state"]
        await oauth.complete_connect(state=state, code=account.id)

    grants = await oauth.list_connections(_NAME)
    assert (await McpServer.get_for_name(_NAME)).identity_mode == IdentityMode.PER_USER
    assert {grant.account_id for grant in grants} == {first.id, second.id}


async def test_a_later_connect_stores_under_the_claimed_mode(auth_server, druks_db):
    first = await Account.get_or_create("first@example.com")
    second = await Account.get_or_create("second@example.com")
    first_url = await oauth.begin_connect(
        _NAME,
        _SERVER_URL,
        _ENDPOINT,
        account_id=first.id,
        identity_mode=IdentityMode.SHARED,
    )
    second_url = await oauth.begin_connect(
        _NAME,
        _SERVER_URL,
        _ENDPOINT,
        account_id=second.id,
        identity_mode=IdentityMode.PER_USER,
    )
    first_state = dict(parse_qsl(urlparse(first_url).query))["state"]
    second_state = dict(parse_qsl(urlparse(second_url).query))["state"]

    await oauth.complete_connect(state=first_state, code="first")
    await oauth.complete_connect(state=second_state, code="second")

    assert (await McpServer.get_for_name(_NAME)).identity_mode == IdentityMode.SHARED
    grant_accounts = {grant.account_id for grant in await oauth.list_connections(_NAME)}
    assert grant_accounts == {SYSTEM_ACCOUNT_ID}


# --- get_access_token: cache, refresh, rotation, eviction -------------------


async def test_get_refreshes_on_cache_miss_and_persists_rotation(auth_server, druks_db):
    await _store_grant(refresh_token="rt-old")
    auth_server.token_response = {
        "access_token": "at-2",
        "refresh_token": "rt-new",
        "expires_in": 300,
    }

    token = await oauth.get_access_token(_NAME, SYSTEM_ACCOUNT_ID)

    assert token == "at-2"
    refresh = auth_server.token_requests[0]
    assert refresh["grant_type"] == "refresh_token"
    # The wire request carries the plaintext; the row carried only ciphertext.
    assert refresh["refresh_token"] == "rt-old"
    assert refresh["resource"] == _SERVER_URL
    # Rotation: the provider's new refresh token replaced the stored one.
    stored = await oauth.get_connection(_NAME, SYSTEM_ACCOUNT_ID)
    assert stored.refresh_token.decrypt() == "rt-new"

    # A second call within the TTL reuses the cache — no second refresh.
    assert await oauth.get_access_token(_NAME, SYSTEM_ACCOUNT_ID) == "at-2"
    assert len(auth_server.token_requests) == 1


async def test_get_without_grant_fails_loudly(druks_db):
    with pytest.raises(MissingGrantError, match=_NAME):
        await oauth.get_access_token(_NAME, SYSTEM_ACCOUNT_ID)


async def test_get_refresh_rejection_fails_loudly_and_evicts_the_cache(auth_server, druks_db):
    await _store_grant()
    auth_server.token_status = 400

    with pytest.raises(GrantRefreshError, match=_NAME):
        await oauth.get_access_token(_NAME, SYSTEM_ACCOUNT_ID)
    assert not await get_client().get(await _token_key(SYSTEM_ACCOUNT_ID))


async def test_get_rejects_a_malformed_token_response(auth_server, druks_db):
    await _store_grant()
    auth_server.token_malformed = True

    with pytest.raises(GrantRefreshError, match="malformed JSON"):
        await oauth.get_access_token(_NAME, SYSTEM_ACCOUNT_ID)


async def test_get_rejects_a_token_response_without_an_access_token(auth_server, druks_db):
    await _store_grant()
    auth_server.token_response = {"refresh_token": "rt-2", "expires_in": 3600}

    with pytest.raises(GrantRefreshError, match="no access token"):
        await oauth.get_access_token(_NAME, SYSTEM_ACCOUNT_ID)


async def test_get_losing_the_refresh_lock_polls_for_the_winners_token(
    auth_server, druks_db, monkeypatch
):
    # A second caller never refreshes (one rotation spender per server) and
    # never blocks the event loop: it polls until the winner's token appears
    # in the cache. The winner here is a concurrent task holding the lock.
    await _store_grant()
    redis = get_client()
    monkeypatch.setattr(oauth, "OAUTH_MINT_WAIT_INTERVAL_SECONDS", 0)
    await redis.set(await _lock_key(SYSTEM_ACCOUNT_ID), "1")

    async def _winner_finishes():
        await redis.set(await _token_key(SYSTEM_ACCOUNT_ID), "at-winner")
        await redis.delete(await _lock_key(SYSTEM_ACCOUNT_ID))

    winner = asyncio.create_task(_winner_finishes())
    assert await oauth.get_access_token(_NAME, SYSTEM_ACCOUNT_ID) == "at-winner"
    await winner
    assert not auth_server.token_requests


async def test_get_times_out_loudly_when_the_refresh_lock_never_frees(druks_db, monkeypatch):
    await _store_grant()
    monkeypatch.setattr(oauth, "OAUTH_MINT_WAIT_INTERVAL_SECONDS", 0)
    monkeypatch.setattr(oauth, "OAUTH_MINT_WAIT_ATTEMPTS", 3)
    await get_client().set(await _lock_key(SYSTEM_ACCOUNT_ID), "1")

    with pytest.raises(GrantRefreshError, match="concurrent refresh"):
        await oauth.get_access_token(_NAME, SYSTEM_ACCOUNT_ID)


async def test_get_cache_and_refresh_lock_are_per_account(auth_server, druks_db):
    first = await Account.get_or_create("first@example.com")
    second = await Account.get_or_create("second@example.com")
    await _store_grant(account_id=first.id, identity_mode=IdentityMode.PER_USER)
    await _store_grant(account_id=second.id, identity_mode=IdentityMode.PER_USER)
    redis = get_client()
    await redis.set(await _lock_key(first.id), "1")

    assert await oauth.get_access_token(_NAME, second.id) == "at-1"
    assert not await redis.get(await _token_key(first.id))
    assert await redis.get(await _token_key(second.id)) == b"at-1"


# --- delivery: the oauth branch of the fold ---------------------------------


async def test_delivery_mints_and_injects_the_oauth_token(registry_state, auth_server, druks_db):
    _register_oauth_server()
    await _store_grant()

    kwargs = await Workspace(host=_FakeSandbox()).with_mcp_servers(  # type: ignore[arg-type]
        SYSTEM_ACCOUNT_ID
    )

    var = get_bearer_token_env_var(_NAME)
    assert kwargs["extra_env"][var] == "at-1"
    entry = next(s for s in kwargs["mcp_servers"] if s.name == _NAME)
    assert entry.url == _SERVER_URL
    assert entry.bearer_token_env_var == var
    assert "at-1" not in repr(entry)


async def test_delivery_fails_loudly_for_an_unconnected_enabled_oauth_server(
    registry_state, druks_db
):
    _register_oauth_server()
    server = await McpServer.create(name=_NAME, url=_SERVER_URL, token_source=TokenSource.OAUTH)
    server.identity_mode = IdentityMode.SHARED

    with pytest.raises(MissingGrantError, match=_NAME):
        await Workspace(host=_FakeSandbox()).with_mcp_servers(  # type: ignore[arg-type]
            SYSTEM_ACCOUNT_ID
        )


async def test_delivery_names_the_account_missing_its_per_user_grant(druks_db):
    account = await Account.get_or_create("run@example.com")
    server = await McpServer.create(name=_NAME, url=_SERVER_URL, token_source=TokenSource.OAUTH)
    server.identity_mode = IdentityMode.PER_USER

    with pytest.raises(MissingGrantError) as error:
        await Workspace(host=_FakeSandbox()).with_mcp_servers(  # type: ignore[arg-type]
            account.id
        )

    assert error.value.name == _NAME
    assert error.value.account_id == account.id
    assert _NAME in str(error.value)
    assert account.id in str(error.value)


async def test_delivery_without_a_run_account_uses_the_fallback(auth_server, druks_db):
    fallback = await Account.get_or_create("fallback@example.com")
    await (await UserSettings.get()).set_fallback_account(fallback.id)
    await _store_grant(account_id=fallback.id, identity_mode=IdentityMode.PER_USER)

    kwargs = await Workspace(host=_FakeSandbox()).with_mcp_servers(  # type: ignore[arg-type]
        None
    )

    assert kwargs["extra_env"][get_bearer_token_env_var(_NAME)] == "at-1"


async def test_delivery_with_a_named_account_does_not_use_the_fallback(auth_server, druks_db):
    fallback = await Account.get_or_create("fallback@example.com")
    named = await Account.get_or_create("named@example.com")
    await (await UserSettings.get()).set_fallback_account(fallback.id)
    await _store_grant(account_id=fallback.id, identity_mode=IdentityMode.PER_USER)

    with pytest.raises(MissingGrantError) as error:
        await Workspace(host=_FakeSandbox()).with_mcp_servers(  # type: ignore[arg-type]
            named.id
        )

    assert error.value.account_id == named.id


# --- API: connect / callback / disconnect / badge ---------------------------


def test_connect_route_requires_druks_endpoint(tmp_path, registry_state, druks_db):
    _register_oauth_server()
    with TestClient(configure_app_for_test(settings=make_settings(tmp_path))) as client:
        response = client.post(
            f"/api/mcp-servers/{_NAME}/connect",
            json={"identity_mode": IdentityMode.SHARED},
        )
        assert response.status_code == 409
        assert "urls.endpoint" in response.text


def test_connect_route_rejects_a_non_oauth_server(tmp_path, druks_db):
    with TestClient(configure_app_for_test(settings=make_settings(tmp_path))) as client:
        assert (
            client.post(
                "/api/mcp-servers/github/connect",
                json={"identity_mode": IdentityMode.SHARED},
            ).status_code
            == 404
        )


def test_connect_route_returns_the_consent_url(tmp_path, registry_state, auth_server, druks_db):
    _register_oauth_server()
    settings = make_settings(tmp_path, urls={"endpoint": _ENDPOINT})
    with TestClient(configure_app_for_test(settings=settings)) as client:
        response = client.post(
            f"/api/mcp-servers/{_NAME}/connect",
            json={"identity_mode": IdentityMode.SHARED},
        )
        assert response.status_code == 200
        assert response.json()["authorizationUrl"].startswith(f"{_AUTH_BASE}/authorize?")


async def test_callback_route_completes_the_connect(
    tmp_path, registry_state, auth_server, druks_db
):
    _register_oauth_server(enabled=False)
    settings = make_settings(tmp_path, urls={"endpoint": _ENDPOINT})

    with TestClient(configure_app_for_test(settings=settings)) as client:
        url = client.post(
            f"/api/mcp-servers/{_NAME}/connect",
            json={"identity_mode": IdentityMode.SHARED},
        ).json()["authorizationUrl"]
        state = dict(parse_qsl(urlparse(url).query))["state"]
        page = client.get("/api/mcp-servers/oauth/callback", params={"state": state, "code": "c"})
        assert page.status_code == 200
        assert "Connected" in page.text
        # The page notifies the opener tab, then closes itself.
        assert "BroadcastChannel('druks-mcp-connect')" in page.text
        assert "window.close()" in page.text
        assert await oauth.get_connection(_NAME, SYSTEM_ACCOUNT_ID)
        # Connecting is the explicit "use this server" — it enables too.
        assert (await McpServer.get_for_name(_NAME)).is_enabled is True

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
    tmp_path, registry_state, auth_server, druks_db
):
    _register_oauth_server()
    await _store_grant()
    await oauth.get_access_token(_NAME, SYSTEM_ACCOUNT_ID)
    token_key = await _token_key(SYSTEM_ACCOUNT_ID)
    # The oauth engine's Redis client is bound to this test's loop; close it so the
    # route dials its own — the cached token lives in Redis either way.
    await close_client()

    with TestClient(configure_app_for_test(settings=make_settings(tmp_path))) as client:
        assert client.delete(f"/api/mcp-servers/{_NAME}/grant").status_code == 204
        assert not await oauth.get_connection(_NAME, SYSTEM_ACCOUNT_ID)
        assert not await McpClientRegistration.get_for_account(_NAME, SYSTEM_ACCOUNT_ID)
        # The mirror of connect-enables: no grant, no calls, so no dead entry
        # riding into VMs.
        assert (await McpServer.get_for_name(_NAME)).is_enabled is False
        assert client.delete(f"/api/mcp-servers/{_NAME}/grant").status_code == 404

    # Read the eviction on this test's own loop. Abandon whatever client the
    # portal left (the conftest fixture's move) rather than trusting lifespan
    # shutdown to have nulled it — a portal-bound client awaited from here
    # parks forever, it doesn't raise.
    druks.redis._client = None
    assert not await get_client().get(token_key)


async def test_shared_disconnect_allows_per_user_reconnect(
    tmp_path, registry_state, auth_server, druks_db
):
    _register_oauth_server()
    operator = await Account.get_or_create("op@example.com")
    settings = make_settings(tmp_path, urls={"endpoint": _ENDPOINT})

    with TestClient(configure_app_for_test(settings=settings)) as client:
        shared_url = client.post(
            f"/api/mcp-servers/{_NAME}/connect",
            json={"identity_mode": IdentityMode.SHARED},
        ).json()["authorizationUrl"]
        shared_state = dict(parse_qsl(urlparse(shared_url).query))["state"]
        assert (
            client.get(
                "/api/mcp-servers/oauth/callback",
                params={"state": shared_state, "code": "shared"},
            ).status_code
            == 200
        )
        assert client.delete(f"/api/mcp-servers/{_NAME}/grant").status_code == 204

        per_user_url = client.post(
            f"/api/mcp-servers/{_NAME}/connect",
            json={"identity_mode": IdentityMode.PER_USER},
        ).json()["authorizationUrl"]
        per_user_state = dict(parse_qsl(urlparse(per_user_url).query))["state"]
        assert (
            client.get(
                "/api/mcp-servers/oauth/callback",
                params={"state": per_user_state, "code": "per-user"},
            ).status_code
            == 200
        )

    assert (await McpServer.get_for_name(_NAME)).identity_mode == IdentityMode.PER_USER
    assert {grant.account_id for grant in await oauth.list_connections(_NAME)} == {operator.id}


async def test_api_has_token_reflects_the_grant_and_leaks_no_secret(
    tmp_path, registry_state, druks_db
):
    _register_oauth_server()
    with TestClient(configure_app_for_test(settings=make_settings(tmp_path))) as client:
        server = next(s for s in client.get("/api/mcp-servers").json() if s["name"] == _NAME)
        assert server["tokenSource"] == "oauth"
        assert server["hasToken"] is False

        await _store_grant(refresh_token="rt-secret-value")
        listed = client.get("/api/mcp-servers")
        server = next(s for s in listed.json() if s["name"] == _NAME)
        assert server["hasToken"] is True
        assert "rt-secret-value" not in listed.text


async def test_connect_route_rejects_a_conflicting_identity_mode(tmp_path, druks_db):
    await _store_grant()

    with TestClient(configure_app_for_test(settings=make_settings(tmp_path))) as client:
        response = client.post(
            f"/api/mcp-servers/{_NAME}/connect",
            json={"identity_mode": IdentityMode.PER_USER},
        )

    assert response.status_code == 409
    assert IdentityMode.SHARED in response.json()["detail"]


async def test_api_has_token_is_scoped_to_the_requesting_account(tmp_path, druks_db):
    connected = await Account.get_or_create("connected@example.com")
    unconnected = await Account.get_or_create("unconnected@example.com")
    await _store_grant(account_id=connected.id, identity_mode=IdentityMode.PER_USER)
    settings = make_settings(
        tmp_path,
        identity={"mode": "header", "header": "X-Test-User"},
    )

    with TestClient(configure_app_for_test(settings=settings, authenticated=False)) as client:
        connected_response = client.get(
            "/api/mcp-servers", headers={"X-Test-User": connected.username}
        )
        unconnected_response = client.get(
            "/api/mcp-servers", headers={"X-Test-User": unconnected.username}
        )

    connected_server = next(
        server for server in connected_response.json() if server["name"] == _NAME
    )
    unconnected_server = next(
        server for server in unconnected_response.json() if server["name"] == _NAME
    )
    assert connected_server["identityMode"] == IdentityMode.PER_USER
    assert connected_server["hasToken"] is True
    assert unconnected_server["identityMode"] == IdentityMode.PER_USER
    assert unconnected_server["hasToken"] is False


async def test_per_user_disconnect_preserves_other_accounts_grant_and_cache(tmp_path, druks_db):
    disconnected = await Account.get_or_create("disconnect@example.com")
    connected = await Account.get_or_create("connected@example.com")
    await _store_grant(account_id=disconnected.id, identity_mode=IdentityMode.PER_USER)
    await _store_grant(account_id=connected.id, identity_mode=IdentityMode.PER_USER)
    redis = get_client()
    disconnected_key = await _token_key(disconnected.id)
    connected_key = await _token_key(connected.id)
    await redis.set(disconnected_key, "disconnect-token")
    await redis.set(connected_key, "connected-token")
    await close_client()
    settings = make_settings(
        tmp_path,
        identity={"mode": "header", "header": "X-Test-User"},
    )

    with TestClient(configure_app_for_test(settings=settings, authenticated=False)) as client:
        response = client.delete(
            f"/api/mcp-servers/{_NAME}/grant",
            headers={"X-Test-User": disconnected.username},
        )
        assert response.status_code == 204

    assert not await oauth.get_connection(_NAME, disconnected.id)
    assert await oauth.get_connection(_NAME, connected.id)
    assert (await McpServer.get_for_name(_NAME)).is_enabled is True
    druks.redis._client = None
    assert not await get_client().get(disconnected_key)
    assert await get_client().get(connected_key) == b"connected-token"


async def test_removal_drops_every_grant_and_cached_token(tmp_path, druks_db):
    first = await Account.get_or_create("first@example.com")
    second = await Account.get_or_create("second@example.com")
    await _store_grant(account_id=first.id, identity_mode=IdentityMode.PER_USER)
    await _store_grant(account_id=second.id, identity_mode=IdentityMode.PER_USER)
    redis = get_client()
    first_key = await _token_key(first.id)
    second_key = await _token_key(second.id)
    await redis.set(first_key, "first-token")
    await redis.set(second_key, "second-token")
    await close_client()

    with TestClient(configure_app_for_test(settings=make_settings(tmp_path))) as client:
        assert client.delete(f"/api/mcp-servers/{_NAME}").status_code == 204

    assert not await McpServer.get_for_name(_NAME)
    assert not await oauth.list_connections(_NAME)
    druks.redis._client = None
    assert not await get_client().get(first_key)
    assert not await get_client().get(second_key)

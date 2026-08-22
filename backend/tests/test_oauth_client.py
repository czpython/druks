import asyncio
import hashlib
from urllib.parse import parse_qsl, urlparse

import httpx
import pytest
from druks.accounts.constants import SYSTEM_ACCOUNT_ID
from druks.database import db_session
from druks.redis import get_client
from druks.services import OauthClient, OauthExchangeError, OauthRefreshError
from druks.services.models import OauthConnection
from druks.services.oauth import complete_connect

_PROVIDER = "acme"
_AUTHORIZATION_ENDPOINT = "https://auth.acme.test/authorize"
_TOKEN_ENDPOINT = "https://auth.acme.test/token"
_REDIRECT_URI = "https://druks.example/api/oauth/callback"


class FakeTokenEndpoint:
    def __init__(self) -> None:
        self.status = 200
        self.response = {"access_token": "at-1", "refresh_token": "rt-1", "expires_in": 3600}
        self.requests: list[dict] = []
        self.authorizations: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(dict(parse_qsl(request.content.decode())))
        self.authorizations.append(request.headers.get("Authorization", ""))
        return httpx.Response(self.status, json=self.response)


@pytest.fixture
def token_endpoint(monkeypatch):
    fake = FakeTokenEndpoint()
    monkeypatch.setattr(
        "druks.services.oauth._http",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)),
    )
    return fake


def _client(**overrides) -> OauthClient:
    kwargs = dict(
        provider=_PROVIDER,
        authorization_endpoint=_AUTHORIZATION_ENDPOINT,
        token_endpoint=_TOKEN_ENDPOINT,
        client_id="client-123",
        client_secret="secret-123",
        mint_wait_interval_seconds=0,
    )
    kwargs.update(overrides)
    return OauthClient(**kwargs)


def _connection(refresh_token: str = "rt-old", scopes: list[str] | None = None) -> OauthConnection:
    return OauthConnection.create(
        provider=_PROVIDER,
        account_id=SYSTEM_ACCOUNT_ID,
        refresh_token=refresh_token,
        scopes=scopes or [],
    )


def _token_key(connection: OauthConnection) -> str:
    return f"{_PROVIDER}:access_token:{connection.id}"


def _scoped_suffix(scopes: tuple[str, ...]) -> str:
    return ":" + hashlib.sha256(" ".join(sorted(scopes)).encode()).hexdigest()[:16]


def _lock_key(connection: OauthConnection) -> str:
    return f"{_PROVIDER}:refresh_lock:{connection.id}"


async def test_get_serves_the_cache_without_a_refresh(token_endpoint):
    connection = _connection()
    await get_client().set(_token_key(connection), "at-cached")

    token = await _client().get_access_token(connection=connection)

    assert token == "at-cached"
    assert not token_endpoint.requests


async def test_get_refreshes_persists_rotation_and_fills_with_skewed_ttl(token_endpoint):
    token_endpoint.response = {"access_token": "at-2", "refresh_token": "rt-new", "expires_in": 300}
    connection = _connection()

    token = await _client().get_access_token(connection=connection)

    assert token == "at-2"
    db_session().expire_all()
    assert OauthConnection.get(connection.id).refresh_token.decrypt() == "rt-new"
    refresh = token_endpoint.requests[0]
    assert refresh["grant_type"] == "refresh_token"
    assert refresh["refresh_token"] == "rt-old"
    # Body-style client auth: the credentials travel in the form.
    assert refresh["client_id"] == "client-123"
    assert refresh["client_secret"] == "secret-123"
    redis = get_client()
    assert await redis.get(_token_key(connection)) == b"at-2"
    assert 0 < await redis.ttl(_token_key(connection)) <= 240
    assert not await redis.get(_lock_key(connection))


async def test_get_fills_the_cache_only_after_the_rotation_is_saved(token_endpoint, monkeypatch):
    token_endpoint.response = {"access_token": "at-2", "refresh_token": "rt-new", "expires_in": 300}
    connection = _connection()

    def _unsavable(self, rotated: str) -> None:
        raise RuntimeError("rotation write failed")

    monkeypatch.setattr(OauthConnection, "_save_refresh_token", _unsavable)
    with pytest.raises(RuntimeError, match="rotation write failed"):
        await _client().get_access_token(connection=connection)

    redis = get_client()
    assert not await redis.get(_token_key(connection))
    assert not await redis.get(_lock_key(connection))


async def test_get_losing_the_lock_polls_for_the_winners_token(token_endpoint):
    connection = _connection()
    redis = get_client()
    await redis.set(_lock_key(connection), "1")

    async def _winner_finishes():
        await redis.set(_token_key(connection), "at-winner")
        await redis.delete(_lock_key(connection))

    winner = asyncio.create_task(_winner_finishes())
    token = await _client().get_access_token(connection=connection)
    await winner

    assert token == "at-winner"
    assert not token_endpoint.requests


async def test_get_times_out_loudly_when_the_lock_never_frees(token_endpoint):
    connection = _connection()
    await get_client().set(_lock_key(connection), "1")

    with pytest.raises(OauthRefreshError, match="concurrent refresh"):
        await _client(mint_wait_attempts=3).get_access_token(connection=connection)


async def test_get_refresh_rejection_evicts_and_raises(token_endpoint):
    token_endpoint.status = 400
    connection = _connection()

    with pytest.raises(OauthRefreshError, match="HTTP 400"):
        await _client().get_access_token(connection=connection)

    assert OauthConnection.get(connection.id).refresh_token.decrypt() == "rt-old"
    redis = get_client()
    assert not await redis.get(_token_key(connection))
    assert not await redis.get(_lock_key(connection))


async def test_get_refresh_uses_basic_auth(token_endpoint):
    connection = _connection()

    await _client(basic_auth=True).get_access_token(connection=connection)

    assert token_endpoint.authorizations[0].startswith("Basic ")
    # Basic auth keeps the client credentials out of the form body.
    assert "client_id" not in token_endpoint.requests[0]
    assert "client_secret" not in token_endpoint.requests[0]


async def test_disconnect_drops_the_connection_and_the_cached_token(token_endpoint):
    connection = _connection()
    await get_client().set(_token_key(connection), "at-cached")

    await _client().disconnect(connection)

    assert not OauthConnection.get(connection.id)
    assert not await get_client().get(_token_key(connection))


async def test_connect_roundtrip_exchanges_with_basic_auth(token_endpoint):
    url = await _client(basic_auth=True).begin_connect(
        redirect_uri=_REDIRECT_URI,
        scopes=("profile.read", "posts.write"),
        context={"account": "a-1"},
        extra_authorize_params={"audience": "api"},
    )

    assert url.startswith(f"{_AUTHORIZATION_ENDPOINT}?")
    params = dict(parse_qsl(urlparse(url).query))
    assert params["scope"] == "profile.read posts.write"
    assert params["audience"] == "api"
    assert params["code_challenge_method"] == "S256"

    # Completion needs only the state: the begun flow's provider and client
    # identity ride the stash.
    tokens, pending = await complete_connect(state=params["state"], code="code-1")

    assert tokens["refresh_token"] == "rt-1"
    assert pending["account"] == "a-1"
    assert pending["provider"] == _PROVIDER
    assert pending["scopes"] == ["profile.read", "posts.write"]
    assert pending["client_id"] == "client-123"
    exchange = token_endpoint.requests[0]
    assert exchange["grant_type"] == "authorization_code"
    assert exchange["code"] == "code-1"
    assert exchange["code_verifier"]
    assert "client_id" not in exchange
    assert "client_secret" not in exchange
    assert token_endpoint.authorizations[0].startswith("Basic ")


async def test_complete_connect_requires_a_refresh_token(token_endpoint):
    token_endpoint.response = {"access_token": "at-1", "expires_in": 3600}
    url = await _client().begin_connect(redirect_uri=_REDIRECT_URI)
    state = dict(parse_qsl(urlparse(url).query))["state"]

    with pytest.raises(OauthExchangeError, match="no refresh token"):
        await complete_connect(state=state, code="code-1")


async def test_downscoped_get_asks_and_caches_apart_from_the_full_grant(token_endpoint):
    connection = _connection(scopes=["posts.write", "profile.read"])
    token_endpoint.response = {
        "access_token": "at-narrow",
        "refresh_token": "rt-1",
        "expires_in": 3600,
        "scope": "profile.read",
    }

    narrow = await _client().get_access_token(connection=connection, scopes=("profile.read",))

    assert narrow == "at-narrow"
    assert token_endpoint.requests[0]["scope"] == "profile.read"
    redis = get_client()
    scoped_key = _token_key(connection) + _scoped_suffix(("profile.read",))
    assert await redis.get(scoped_key) == b"at-narrow"
    assert not await redis.get(_token_key(connection))

    token_endpoint.response = {
        "access_token": "at-full",
        "refresh_token": "rt-1",
        "expires_in": 3600,
    }
    full = await _client().get_access_token(connection=connection)

    assert full == "at-full"
    assert "scope" not in token_endpoint.requests[1]
    assert await redis.get(_token_key(connection)) == b"at-full"
    assert await redis.get(scoped_key) == b"at-narrow"

    # The scoped cache serves the scoped ask without another refresh.
    assert (
        await _client().get_access_token(connection=connection, scopes=("profile.read",))
        == "at-narrow"
    )
    assert len(token_endpoint.requests) == 2


async def test_downscoped_get_rejects_scopes_outside_the_grant(token_endpoint):
    connection = _connection(scopes=["profile.read"])

    with pytest.raises(OauthRefreshError, match="does not grant scope"):
        await _client().get_access_token(connection=connection, scopes=("posts.write",))

    assert not token_endpoint.requests


async def test_downscoped_get_rejects_a_provider_that_ignores_the_ask(token_endpoint):
    connection = _connection(scopes=["posts.write", "profile.read"])
    token_endpoint.response = {
        "access_token": "at-broad",
        "refresh_token": "rt-1",
        "expires_in": 3600,
        "scope": "posts.write profile.read",
    }

    with pytest.raises(OauthRefreshError, match="came back with"):
        await _client().get_access_token(connection=connection, scopes=("profile.read",))

    scoped_key = _token_key(connection) + _scoped_suffix(("profile.read",))
    assert not await get_client().get(scoped_key)


async def test_uncached_get_refreshes_past_a_live_cache_and_refills_it(token_endpoint):
    connection = _connection()
    redis = get_client()
    await redis.set(_token_key(connection), "at-tail")

    token = await _client().get_access_token(connection=connection, cached=False)

    assert token == "at-1"
    assert len(token_endpoint.requests) == 1
    assert await redis.get(_token_key(connection)) == b"at-1"
    assert not await redis.get(_lock_key(connection))


async def test_refresher_election_is_per_scope_set(token_endpoint):
    connection = _connection(scopes=["profile.read"])
    redis = get_client()
    await redis.set(_lock_key(connection) + _scoped_suffix(("profile.read",)), "1")

    # The scoped variant's lock never blocks the full-grant mint.
    assert await _client().get_access_token(connection=connection) == "at-1"
    assert len(token_endpoint.requests) == 1


async def test_disconnect_evicts_the_scope_variant_keys(token_endpoint):
    connection = _connection(scopes=["profile.read"])
    redis = get_client()
    scoped_key = _token_key(connection) + _scoped_suffix(("profile.read",))
    await redis.set(_token_key(connection), "at-full")
    await redis.set(scoped_key, "at-narrow")

    await _client().disconnect(connection)

    assert not await redis.get(_token_key(connection))
    assert not await redis.get(scoped_key)

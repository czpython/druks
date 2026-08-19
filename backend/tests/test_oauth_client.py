import asyncio
from urllib.parse import parse_qsl, urlparse

import httpx
import pytest
from druks.redis import get_client
from druks.services import OauthClient, OauthExchangeError, OauthRefreshError

_PROVIDER = "acme"
_AUTHORIZATION_ENDPOINT = "https://auth.acme.test/authorize"
_TOKEN_ENDPOINT = "https://auth.acme.test/token"
_REDIRECT_URI = "https://druks.example/api/acme/oauth/callback"
_TOKEN_KEY = f"{_PROVIDER}:access_token:grant-1"
_LOCK_KEY = f"{_PROVIDER}:refresh_lock:grant-1"


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
def token_endpoint():
    return FakeTokenEndpoint()


def _client(token_endpoint: FakeTokenEndpoint, **overrides) -> OauthClient:
    kwargs = dict(
        provider=_PROVIDER,
        authorization_endpoint=_AUTHORIZATION_ENDPOINT,
        token_endpoint=_TOKEN_ENDPOINT,
        client_id="client-123",
        client_secret="secret-123",
        mint_wait_interval_seconds=0,
        http_factory=lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(token_endpoint.handler)
        ),
    )
    kwargs.update(overrides)
    return OauthClient(**kwargs)


def _fail_save(token: str) -> None:
    pytest.fail("nothing rotated")


async def test_mint_serves_the_cache_without_a_refresh(token_endpoint):
    await get_client().set(_TOKEN_KEY, "at-cached")

    token = await _client(token_endpoint).mint_access_token(
        key="grant-1",
        load_refresh_token=lambda: pytest.fail("cache hit reads no grant"),
        save_refresh_token=_fail_save,
    )

    assert token == "at-cached"
    assert not token_endpoint.requests


async def test_mint_refreshes_persists_rotation_and_fills_with_skewed_ttl(token_endpoint):
    token_endpoint.response = {"access_token": "at-2", "refresh_token": "rt-new", "expires_in": 300}
    saved: list[str] = []

    token = await _client(token_endpoint).mint_access_token(
        key="grant-1",
        load_refresh_token=lambda: "rt-old",
        save_refresh_token=saved.append,
    )

    assert token == "at-2"
    assert saved == ["rt-new"]
    refresh = token_endpoint.requests[0]
    assert refresh["grant_type"] == "refresh_token"
    assert refresh["refresh_token"] == "rt-old"
    # Body-style client auth: the credentials travel in the form.
    assert refresh["client_id"] == "client-123"
    assert refresh["client_secret"] == "secret-123"
    redis = get_client()
    assert await redis.get(_TOKEN_KEY) == b"at-2"
    assert 0 < await redis.ttl(_TOKEN_KEY) <= 240
    assert not await redis.get(_LOCK_KEY)


async def test_mint_fills_the_cache_only_after_the_rotation_is_saved(token_endpoint):
    token_endpoint.response = {"access_token": "at-2", "refresh_token": "rt-new", "expires_in": 300}

    def save_refresh_token(token: str) -> None:
        raise RuntimeError("rotation write failed")

    with pytest.raises(RuntimeError, match="rotation write failed"):
        await _client(token_endpoint).mint_access_token(
            key="grant-1",
            load_refresh_token=lambda: "rt-old",
            save_refresh_token=save_refresh_token,
        )

    redis = get_client()
    assert not await redis.get(_TOKEN_KEY)
    assert not await redis.get(_LOCK_KEY)


async def test_mint_losing_the_lock_polls_for_the_winners_token(token_endpoint):
    redis = get_client()
    await redis.set(_LOCK_KEY, "1")

    async def _winner_finishes():
        await redis.set(_TOKEN_KEY, "at-winner")
        await redis.delete(_LOCK_KEY)

    winner = asyncio.create_task(_winner_finishes())
    token = await _client(token_endpoint).mint_access_token(
        key="grant-1",
        load_refresh_token=lambda: "rt-old",
        save_refresh_token=_fail_save,
    )
    await winner

    assert token == "at-winner"
    assert not token_endpoint.requests


async def test_mint_times_out_loudly_when_the_lock_never_frees(token_endpoint):
    await get_client().set(_LOCK_KEY, "1")

    with pytest.raises(OauthRefreshError, match="concurrent refresh"):
        await _client(token_endpoint, mint_wait_attempts=3).mint_access_token(
            key="grant-1",
            load_refresh_token=lambda: "rt-old",
            save_refresh_token=_fail_save,
        )


async def test_mint_refresh_rejection_evicts_and_raises(token_endpoint):
    token_endpoint.status = 400

    with pytest.raises(OauthRefreshError, match="HTTP 400"):
        await _client(token_endpoint).mint_access_token(
            key="grant-1",
            load_refresh_token=lambda: "rt-old",
            save_refresh_token=_fail_save,
        )

    redis = get_client()
    assert not await redis.get(_TOKEN_KEY)
    assert not await redis.get(_LOCK_KEY)


async def test_mint_refresh_uses_basic_auth(token_endpoint):
    await _client(token_endpoint, basic_auth=True).mint_access_token(
        key="grant-1",
        load_refresh_token=lambda: "rt-old",
        save_refresh_token=lambda token: None,
    )

    assert token_endpoint.authorizations[0].startswith("Basic ")
    # Basic auth keeps the client credentials out of the form body.
    assert "client_id" not in token_endpoint.requests[0]
    assert "client_secret" not in token_endpoint.requests[0]


async def test_connect_roundtrip_exchanges_with_basic_auth(token_endpoint):
    url = await _client(token_endpoint, basic_auth=True).begin_connect(
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

    # Completion needs only the provider: the begun flow's client identity
    # rides the stashed state.
    tokens, context = await OauthClient(
        provider=_PROVIDER,
        http_factory=lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(token_endpoint.handler)
        ),
    ).complete_connect(state=params["state"], code="code-1")

    assert tokens["refresh_token"] == "rt-1"
    assert context["account"] == "a-1"
    assert context["client_id"] == "client-123"
    exchange = token_endpoint.requests[0]
    assert exchange["grant_type"] == "authorization_code"
    assert exchange["code"] == "code-1"
    assert exchange["code_verifier"]
    assert "client_id" not in exchange
    assert "client_secret" not in exchange
    assert token_endpoint.authorizations[0].startswith("Basic ")


async def test_complete_connect_requires_a_refresh_token(token_endpoint):
    token_endpoint.response = {"access_token": "at-1", "expires_in": 3600}
    url = await _client(token_endpoint).begin_connect(redirect_uri=_REDIRECT_URI)
    state = dict(parse_qsl(urlparse(url).query))["state"]

    with pytest.raises(OauthExchangeError, match="no refresh token"):
        await _client(token_endpoint).complete_connect(state=state, code="code-1")

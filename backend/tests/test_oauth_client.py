import hashlib
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from urllib.parse import parse_qsl, urlparse

import httpx
import pytest
from druks.core.services import Github
from druks.db import db_session
from druks.redis import get_client
from druks.secrets.datastructures import Audience
from druks.secrets.enums import SecretKind
from druks.secrets.models import VaultSecret
from druks.services import OauthClient, OauthRefreshError
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


async def _connection(
    refresh_token: str = "rt-old", scopes: list[str] | None = None
) -> VaultSecret:
    return await VaultSecret.connect(
        db_session(),
        Audience.service(_PROVIDER),
        account_id=None,
        refresh_token=refresh_token,
        scopes=scopes or [],
    )


def _scope_hash(scopes: tuple[str, ...]) -> str:
    return hashlib.sha256(" ".join(sorted(scopes)).encode()).hexdigest()[:16]


def _audience(connection: VaultSecret, scopes: tuple[str, ...] = ()) -> str:
    if scopes:
        return Audience.oauth(_PROVIDER, connection.id, _scope_hash(scopes))
    return Audience.oauth(_PROVIDER, connection.id)


async def _keep(
    connection: VaultSecret,
    value: str,
    scopes: tuple[str, ...] = (),
    lasts: timedelta = timedelta(hours=1),
) -> None:
    await VaultSecret.store(
        db_session(),
        SecretKind.STATIC,
        _audience(connection, scopes),
        secrets={"value": value},
        expires_at=datetime.now(UTC) + lasts,
    )


async def _kept(connection: VaultSecret, scopes: tuple[str, ...] = ()) -> tuple | None:
    return await VaultSecret.get_token(db_session(), _audience(connection, scopes))


def _lock_key(connection: VaultSecret) -> str:
    return f"{_PROVIDER}:refresh_lock:{connection.id}"


async def test_get_serves_the_kept_token_without_a_refresh(token_endpoint):
    connection = await _connection()
    await _keep(connection, "at-kept")

    token, expires_at = await _client().get_access_token(db_session(), connection=connection)

    assert token == "at-kept"
    assert expires_at - datetime.now(UTC) > timedelta(minutes=59)
    assert not token_endpoint.requests


async def test_get_refreshes_persists_rotation_and_keeps_the_token_in_the_vault(token_endpoint):
    # Longer than TOKEN_MIN_TTL_SECONDS: utc_now() drops microseconds, so a token
    # that lasts exactly the minimum is kept only within the second of its refresh.
    token_endpoint.response = {"access_token": "at-2", "refresh_token": "rt-new", "expires_in": 600}
    connection = await _connection()

    token, expires_at = await _client().get_access_token(db_session(), connection=connection)

    assert token == "at-2"
    # The expiry is the provider's 600s from now.
    assert timedelta(seconds=590) < expires_at - datetime.now(UTC) <= timedelta(seconds=600)
    db_session().expunge_all()
    assert (await db_session().get(VaultSecret, connection.id)).secrets["refresh_token"] == "rt-new"
    refresh = token_endpoint.requests[0]
    assert refresh["grant_type"] == "refresh_token"
    assert refresh["refresh_token"] == "rt-old"
    # Body-style client auth: the credentials travel in the form.
    assert refresh["client_id"] == "client-123"
    assert refresh["client_secret"] == "secret-123"
    assert (await _kept(connection))[0] == "at-2"
    # Redis holds no token, and the refresh lock is gone.
    assert not await get_client().keys()


async def test_get_keeps_the_token_only_after_the_rotation_is_saved(token_endpoint, monkeypatch):
    token_endpoint.response = {"access_token": "at-2", "refresh_token": "rt-new", "expires_in": 900}
    connection = await _connection()

    def _unsavable(self, rotated: str) -> None:
        raise RuntimeError("rotation write failed")

    monkeypatch.setattr(VaultSecret, "update_refresh_token", _unsavable)
    with pytest.raises(RuntimeError, match="rotation write failed"):
        await _client().get_access_token(db_session(), connection=connection)

    assert not await _kept(connection)
    assert not await get_client().get(_lock_key(connection))


async def test_get_losing_the_lock_polls_for_the_winners_token(token_endpoint, monkeypatch):
    connection = await _connection()
    await get_client().set(_lock_key(connection), "1")

    async def winner_keeps_its_token(seconds: float) -> None:
        await _keep(connection, "at-winner")

    # The winner holds its lock and keeps its token while the loser waits.
    monkeypatch.setattr(
        "druks.services.oauth.asyncio", SimpleNamespace(sleep=winner_keeps_its_token)
    )
    token, _ = await _client().get_access_token(db_session(), connection=connection)

    assert token == "at-winner"
    assert not token_endpoint.requests


async def test_get_times_out_loudly_when_the_lock_never_frees(token_endpoint):
    connection = await _connection()
    await get_client().set(_lock_key(connection), "1")

    with pytest.raises(OauthRefreshError, match="concurrent refresh"):
        await _client(mint_wait_attempts=3).get_access_token(db_session(), connection=connection)


async def test_get_refresh_rejection_keeps_nothing_and_raises(token_endpoint):
    token_endpoint.status = 400
    connection = await _connection()

    with pytest.raises(OauthRefreshError, match="HTTP 400"):
        await _client().get_access_token(db_session(), connection=connection)

    assert (await db_session().get(VaultSecret, connection.id)).secrets["refresh_token"] == "rt-old"
    assert not await _kept(connection)
    assert not await get_client().get(_lock_key(connection))


@pytest.mark.parametrize(("status", "response"), [(500, {}), (400, {"error": "invalid_client"})])
async def test_other_refresh_failures_leave_the_connection_live(
    token_endpoint, monkeypatch, status, response
):
    token_endpoint.status = status
    token_endpoint.response = response
    published = []

    async def record(name, **kwargs):
        published.append(name)

    monkeypatch.setattr("druks.services.oauth.publish", record)
    connection = await _connection()

    with pytest.raises(OauthRefreshError, match=f"HTTP {status}"):
        await _client().get_access_token(db_session(), connection=connection)

    assert await VaultSecret.reload(db_session(), connection.id)
    assert not published


async def test_a_service_reads_its_own_revoked_answer(token_endpoint, monkeypatch):
    token_endpoint.response = {"error": "bad_refresh_token", "error_description": "expired"}
    client = _client(is_grant_revoked=Github.Authentication.is_grant_revoked)
    published = []

    async def record(name, **kwargs):
        published.append(name)

    monkeypatch.setattr("druks.services.oauth.publish", record)
    connection = await _connection()

    with pytest.raises(OauthRefreshError, match="revoked the grant"):
        await client.get_access_token(db_session(), connection=connection)

    assert not await VaultSecret.reload(db_session(), connection.id)
    assert published == ["oauth.disconnected"]


async def test_get_refresh_uses_basic_auth(token_endpoint):
    connection = await _connection()

    await _client(basic_auth=True).get_access_token(db_session(), connection=connection)

    assert token_endpoint.authorizations[0].startswith("Basic ")
    # Basic auth keeps the client credentials out of the form body.
    assert "client_id" not in token_endpoint.requests[0]
    assert "client_secret" not in token_endpoint.requests[0]


async def test_disconnect_revokes_the_connection_and_its_kept_token(token_endpoint):
    connection = await _connection()
    await _keep(connection, "at-kept")

    await _client().disconnect(connection, reason="user")

    revoked = await db_session().get(VaultSecret, connection.id)
    assert revoked.revoked_at
    assert revoked.revoked_reason == "user"
    # Nothing secret outlives the consent at rest.
    assert "refresh_token" not in revoked.secrets
    assert revoked.account_id is None
    assert not await _kept(connection)

    # A second revoke keeps the first stamp.
    first_stamp = revoked.revoked_at
    await revoked.revoke("client_replaced")
    assert revoked.revoked_at == first_stamp
    assert revoked.revoked_reason == "user"


async def test_a_revoke_landing_mid_refresh_is_not_overwritten(token_endpoint):
    connection = await _connection()
    exchange = token_endpoint.handler

    async def revoke_then_rotate(request: httpx.Request) -> httpx.Response:
        await connection.revoke("user")
        return exchange(request)

    token_endpoint.handler = revoke_then_rotate

    with pytest.raises(OauthRefreshError, match="revoked mid-refresh"):
        await _client().get_access_token(db_session(), connection=connection)

    # The rotated token is not stored and no access token is kept.
    assert "refresh_token" not in (await db_session().get(VaultSecret, connection.id)).secrets
    assert not await _kept(connection)


async def test_get_refuses_a_revoked_connection(token_endpoint):
    connection = await _connection()
    await connection.revoke("user")

    with pytest.raises(OauthRefreshError, match="revoked"):
        await _client().get_access_token(db_session(), connection=connection)

    assert not token_endpoint.requests


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

    # Completion needs only the state. The stash holds the flow's provider and client.
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


async def test_begin_connect_call_params_override_the_declared_ones():
    client = _client(extra_authorize_params={"access_type": "offline", "prompt": "consent"})

    url = await client.begin_connect(
        redirect_uri=_REDIRECT_URI, extra_authorize_params={"prompt": "select_account"}
    )

    params = dict(parse_qsl(urlparse(url).query))
    assert params["access_type"] == "offline"
    assert params["prompt"] == "select_account"


async def test_downscoped_get_asks_and_keeps_apart_from_the_full_grant(token_endpoint):
    connection = await _connection(scopes=["posts.write", "profile.read"])
    token_endpoint.response = {
        "access_token": "at-narrow",
        "refresh_token": "rt-1",
        "expires_in": 3600,
        "scope": "profile.read",
    }

    narrow, _ = await _client().get_access_token(
        db_session(), connection=connection, scopes=("profile.read",)
    )

    assert narrow == "at-narrow"
    assert token_endpoint.requests[0]["scope"] == "profile.read"
    assert (await _kept(connection, ("profile.read",)))[0] == "at-narrow"
    assert not await _kept(connection)

    token_endpoint.response = {
        "access_token": "at-full",
        "refresh_token": "rt-1",
        "expires_in": 3600,
    }
    full, _ = await _client().get_access_token(db_session(), connection=connection)

    assert full == "at-full"
    assert "scope" not in token_endpoint.requests[1]
    assert (await _kept(connection))[0] == "at-full"
    assert (await _kept(connection, ("profile.read",)))[0] == "at-narrow"

    # The scoped token serves the scoped ask without another refresh.
    assert (
        await _client().get_access_token(
            db_session(), connection=connection, scopes=("profile.read",)
        )
    )[0] == "at-narrow"
    assert len(token_endpoint.requests) == 2


async def test_downscoped_get_rejects_scopes_outside_the_grant(token_endpoint):
    connection = await _connection(scopes=["profile.read"])

    with pytest.raises(OauthRefreshError, match="does not grant scope"):
        await _client().get_access_token(
            db_session(), connection=connection, scopes=("posts.write",)
        )

    assert not token_endpoint.requests


async def test_downscoped_get_rejects_a_provider_that_ignores_the_ask(token_endpoint):
    connection = await _connection(scopes=["posts.write", "profile.read"])
    token_endpoint.response = {
        "access_token": "at-broad",
        "refresh_token": "rt-1",
        "expires_in": 3600,
        "scope": "posts.write profile.read",
    }

    with pytest.raises(OauthRefreshError, match="came back with"):
        await _client().get_access_token(
            db_session(), connection=connection, scopes=("profile.read",)
        )

    assert not await _kept(connection, ("profile.read",))


async def test_get_refreshes_a_kept_token_that_ends_within_the_min_ttl(token_endpoint):
    connection = await _connection()
    await _keep(connection, "at-tail", lasts=timedelta(minutes=10))

    token, _ = await _client().get_access_token(
        db_session(), connection=connection, min_ttl_seconds=30 * 60
    )

    assert token == "at-1"
    assert len(token_endpoint.requests) == 1
    assert (await _kept(connection))[0] == "at-1"
    assert not await get_client().get(_lock_key(connection))


async def test_a_fresh_token_shorter_than_the_min_ttl_serves_with_a_warning(token_endpoint, caplog):
    token_endpoint.response = {"access_token": "at-2", "refresh_token": "rt-1", "expires_in": 600}
    connection = await _connection()

    token, _ = await _client().get_access_token(
        db_session(), connection=connection, min_ttl_seconds=30 * 60
    )

    assert token == "at-2"
    assert "under the 1800 asked" in caplog.text


async def test_refresher_election_is_per_scope_set(token_endpoint):
    connection = await _connection(scopes=["profile.read"])
    redis = get_client()
    await redis.set(f"{_lock_key(connection)}:{_scope_hash(('profile.read',))}", "1")

    # The scoped variant's lock never blocks the full-grant mint.
    assert (await _client().get_access_token(db_session(), connection=connection))[0] == "at-1"
    assert len(token_endpoint.requests) == 1


async def test_disconnect_revokes_the_scope_variants(token_endpoint):
    connection = await _connection(scopes=["profile.read"])
    await _keep(connection, "at-full")
    await _keep(connection, "at-narrow", ("profile.read",))

    await _client().disconnect(connection, reason="user")

    assert not await _kept(connection)
    assert not await _kept(connection, ("profile.read",))

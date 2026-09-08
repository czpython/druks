import asyncio
import base64
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import druks.redis
import httpx
import pytest
from conftest import connect_provider
from druks.accounts.models import Account
from druks.core import tasks
from druks.database import db_session
from druks.harnesses import providers as pbase
from druks.harnesses.datastructures import ParsedUsage
from druks.harnesses.exceptions import HarnessNotConnectedError, OAuthTokenError
from druks.harnesses.providers import AnthropicProvider, OpenAiProvider
from druks.sandbox import gate
from druks.sandbox.models import SandboxIdentity, SecretRef
from druks.secrets.datastructures import Audience
from druks.secrets.enums import SecretKind
from druks.secrets.models import VaultSecret
from druks.testing import seed_run
from druks_field_notes.workflows import Summarize
from sqlalchemy import update

_NOW = datetime(2026, 6, 4, 20, 0, tzinfo=UTC)


def _jwt(exp: int) -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').rstrip(b"=").decode()
    payload = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).rstrip(b"=").decode()
    return f"{header}.{payload}.sig"


def _claude_payload(*, access="A0", refresh="R0", expires_at=None, extra=None) -> dict:
    block = {"accessToken": access, "scopes": ["user:profile"], "subscriptionType": "max"}
    if refresh is not None:
        block["refreshToken"] = refresh
    if expires_at is not None:
        block["expiresAt"] = int(expires_at.timestamp() * 1000)
    if extra:
        block.update(extra)
    return {"claudeAiOauth": block}


async def _seed_claude(*, provider_email="op@example.com", **kwargs) -> VaultSecret:
    return await connect_provider(
        AnthropicProvider, _claude_payload(**kwargs), provider_email=provider_email
    )


def _codex_payload(*, access=None, refresh="R0", account_id="acc-1", id_token="id-0") -> dict:
    access = access or _jwt(int((_NOW + timedelta(days=9)).timestamp()))
    tokens = {"access_token": access, "id_token": id_token, "account_id": account_id}
    if refresh is not None:
        tokens["refresh_token"] = refresh
    return {"auth_mode": "chatgpt", "OPENAI_API_KEY": None, "tokens": tokens}


async def _seed_codex(*, provider_email="op@example.com", **kwargs) -> VaultSecret:
    return await connect_provider(
        OpenAiProvider, _codex_payload(**kwargs), provider_email=provider_email
    )


async def _payload(provider_id: str) -> dict:
    # Rotation commits in its own session; refresh past this session's identity map.
    row = await VaultSecret.lookup(
        SecretKind.SUBSCRIPTION, Audience.provider(provider_id), (await Account.get_default()).id
    )
    await db_session().refresh(row)
    return row.secrets


def _resp(status: int, body: object) -> httpx.Response:
    text = body if isinstance(body, str) else json.dumps(body)
    return httpx.Response(status, text=text, request=httpx.Request("GET", "https://x"))


def _mock_post(monkeypatch, response):
    calls = []

    async def fake_post(self, url, *, json=None, **_kwargs):
        calls.append({"url": url, "json": json})
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr(pbase.httpx.AsyncClient, "post", fake_post)
    return calls


def _mock_get(monkeypatch, response):
    calls = []

    async def fake_get(self, url, *, headers=None, **_kwargs):
        calls.append({"url": url, "headers": headers})
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr(pbase.httpx.AsyncClient, "get", fake_get)
    return calls


async def test_claude_load_token(druks_db):
    connection = await _seed_claude(access="live", expires_at=_NOW + timedelta(hours=2))
    token = AnthropicProvider.load_token(connection, now=_NOW)
    assert token.access_token == "live"
    assert token.subscription_type == "max"
    assert "user:profile" in token.scopes


async def test_claude_load_token_expired(druks_db):
    connection = await _seed_claude(expires_at=_NOW - timedelta(hours=1))
    with pytest.raises(OAuthTokenError) as e:
        AnthropicProvider.load_token(connection, now=_NOW)
    assert e.value.tag == "token_expired"


async def test_claude_load_token_no_access(druks_db):
    connection = await connect_provider(
        AnthropicProvider, {"claudeAiOauth": {"subscriptionType": "max"}}
    )
    with pytest.raises(OAuthTokenError) as e:
        AnthropicProvider.load_token(connection, now=_NOW)
    assert e.value.tag == "no_token"


async def test_codex_load_token(druks_db):
    connection = await _seed_codex()
    token = OpenAiProvider.load_token(connection, now=_NOW)
    assert "." in token.access_token
    assert token.account_id == "acc-1"


async def test_codex_load_token_expired(druks_db):
    connection = await _seed_codex(access=_jwt(int((_NOW - timedelta(hours=1)).timestamp())))
    with pytest.raises(OAuthTokenError) as e:
        OpenAiProvider.load_token(connection, now=_NOW)
    assert e.value.tag == "token_expired"


async def test_claude_fresh_not_refreshed(monkeypatch, druks_db):
    connection = await _seed_claude(expires_at=_NOW + timedelta(hours=6))
    calls = _mock_post(monkeypatch, _resp(200, {}))
    result = await AnthropicProvider.rotate_token(connection.id, now=_NOW)
    assert result.action == "fresh"
    assert result.subscription_id == connection.id
    assert calls == []


async def test_claude_stale_refreshes_and_persists(monkeypatch, druks_db):
    soon = _NOW + timedelta(minutes=30)
    connection = await _seed_claude(access="old", refresh="R0", expires_at=soon)
    calls = _mock_post(
        monkeypatch, _resp(200, {"access_token": "new", "refresh_token": "R1", "expires_in": 28800})
    )
    result = await AnthropicProvider.rotate_token(connection.id, now=_NOW)
    assert result.action == "refreshed"
    assert calls[0]["json"]["refresh_token"] == "R0"
    block = (await _payload("anthropic"))["claudeAiOauth"]
    assert block["accessToken"] == "new"
    assert block["refreshToken"] == "R1"
    assert block["scopes"] == ["user:profile"]  # preserved
    assert block["subscriptionType"] == "max"  # preserved
    assert block["expiresAt"] == int((_NOW + timedelta(seconds=28800)).timestamp() * 1000)


async def test_claude_invalid_grant_drops_row(monkeypatch, druks_db):
    connection = await _seed_claude(access="old", expires_at=_NOW - timedelta(minutes=1))
    account_id = connection.account_id
    _mock_post(monkeypatch, _resp(400, {"error": "invalid_grant"}))
    result = await AnthropicProvider.rotate_token(connection.id, now=_NOW)
    assert result.action == "failed"
    assert result.error == "invalid_grant"
    # A revoked lineage self-disconnects and commits inside the rotation — the
    # deletion never rides (or rolls back with) the tick's later commit.
    assert not await VaultSecret.list_subscriptions()
    with pytest.raises(HarnessNotConnectedError):
        await AnthropicProvider.get_subscription(account_id)


async def test_claude_network_error_keeps_row(monkeypatch, druks_db):
    connection = await _seed_claude(access="old", expires_at=_NOW - timedelta(minutes=1))
    _mock_post(monkeypatch, httpx.ConnectError("boom"))
    result = await AnthropicProvider.rotate_token(connection.id, now=_NOW)
    assert result.error == "network"
    assert (await _payload("anthropic"))["claudeAiOauth"]["accessToken"] == "old"


async def test_claude_http_500_keeps_row(monkeypatch, druks_db):
    connection = await _seed_claude(access="old", expires_at=_NOW - timedelta(minutes=1))
    _mock_post(monkeypatch, _resp(500, ""))
    result = await AnthropicProvider.rotate_token(connection.id, now=_NOW)
    assert result.error == "http_500"
    assert (await _payload("anthropic"))["claudeAiOauth"]["accessToken"] == "old"


async def test_claude_bad_response_keeps_row(monkeypatch, druks_db):
    connection = await _seed_claude(access="old", expires_at=_NOW - timedelta(minutes=1))
    _mock_post(monkeypatch, _resp(200, "not json"))
    result = await AnthropicProvider.rotate_token(connection.id, now=_NOW)
    assert result.error == "bad_response"
    assert (await _payload("anthropic"))["claudeAiOauth"]["accessToken"] == "old"


async def test_rotation_of_a_deleted_row_is_a_no_op(monkeypatch, druks_db):
    connection = await _seed_claude(access="old", expires_at=_NOW - timedelta(minutes=1))
    connection_id = connection.id
    _mock_post(monkeypatch, _resp(400, {"error": "invalid_grant"}))
    await AnthropicProvider.rotate_token(connection_id, now=_NOW)
    # Row is gone; rotating the stale id must short-circuit before any
    # identity POST.
    calls = _mock_post(monkeypatch, _resp(200, {"access_token": "x"}))
    result = await AnthropicProvider.rotate_token(connection_id, now=_NOW)
    assert result.action == "failed"
    assert result.error == "no_credentials"
    assert calls == []


async def test_claude_relogin_overwrite_picked_up(monkeypatch, druks_db):
    connection = await _seed_claude(refresh="R_NEW", expires_at=_NOW - timedelta(minutes=1))
    calls = _mock_post(
        monkeypatch, _resp(200, {"access_token": "a", "refresh_token": "b", "expires_in": 100})
    )
    await AnthropicProvider.rotate_token(connection.id, now=_NOW)
    assert calls[0]["json"]["refresh_token"] == "R_NEW"


async def test_codex_stale_refreshes_and_preserves(monkeypatch, druks_db):
    stale = _jwt(int((_NOW + timedelta(hours=1)).timestamp()))
    fresh = _jwt(int((_NOW + timedelta(days=10)).timestamp()))
    connection = await _seed_codex(access=stale, refresh="R0", account_id="acc-9")
    calls = _mock_post(
        monkeypatch, _resp(200, {"access_token": fresh, "refresh_token": "R1", "id_token": "id-1"})
    )
    result = await OpenAiProvider.rotate_token(connection.id, now=_NOW)
    assert result.action == "refreshed"
    assert calls[0]["json"]["client_id"] == "app_EMoamEEZ73f0CkXaXp7hrann"
    data = await _payload("openai")
    assert data["tokens"]["access_token"] == fresh
    assert data["tokens"]["refresh_token"] == "R1"
    assert data["tokens"]["id_token"] == "id-1"
    assert data["tokens"]["account_id"] == "acc-9"  # preserved
    assert data["auth_mode"] == "chatgpt"  # preserved
    assert "last_refresh" in data


async def test_codex_keeps_refresh_when_omitted(monkeypatch, druks_db):
    stale = _jwt(int((_NOW + timedelta(hours=1)).timestamp()))
    fresh = _jwt(int((_NOW + timedelta(days=10)).timestamp()))
    connection = await _seed_codex(access=stale, refresh="KEEP")
    _mock_post(monkeypatch, _resp(200, {"access_token": fresh}))
    await OpenAiProvider.rotate_token(connection.id, now=_NOW)
    assert (await _payload("openai"))["tokens"]["refresh_token"] == "KEEP"


async def test_codex_no_refresh_token(monkeypatch, druks_db):
    stale = _jwt(int((_NOW + timedelta(hours=1)).timestamp()))
    connection = await _seed_codex(refresh=None, access=stale)
    calls = _mock_post(monkeypatch, _resp(200, {}))
    result = await OpenAiProvider.rotate_token(connection.id, now=_NOW)
    assert result.action == "no_refresh_token"
    assert calls == []


async def test_rotation_touches_only_the_addressed_row(monkeypatch, druks_db):
    stale = await _seed_claude(
        access="old",
        refresh="R0",
        expires_at=_NOW + timedelta(minutes=30),
        provider_email="a@example.com",
    )
    other = await _seed_claude(
        access="keep",
        refresh="RK",
        expires_at=_NOW + timedelta(minutes=30),
        provider_email="b@example.com",
    )
    stale_id, other_id = stale.id, other.id
    _mock_post(
        monkeypatch, _resp(200, {"access_token": "new", "refresh_token": "R1", "expires_in": 100})
    )
    result = await AnthropicProvider.rotate_token(stale_id, now=_NOW)
    assert result.action == "refreshed"
    assert (
        dict((await VaultSecret.reload(stale_id)).secrets)["claudeAiOauth"]["accessToken"] == "new"
    )
    assert (
        dict((await VaultSecret.reload(other_id)).secrets)["claudeAiOauth"]["accessToken"] == "keep"
    )


async def test_invalid_grant_drops_only_the_addressed_row(monkeypatch, druks_db):
    kept = await _seed_claude(access="d", expires_at=_NOW - timedelta(minutes=1))
    other = await _seed_claude(
        access="o", expires_at=_NOW - timedelta(minutes=1), provider_email="b@example.com"
    )
    kept_id, other_id = kept.id, other.id
    _mock_post(monkeypatch, _resp(400, {"error": "invalid_grant"}))
    await AnthropicProvider.rotate_token(other_id, now=_NOW)
    assert not await VaultSecret.reload(other_id)
    assert await VaultSecret.reload(kept_id)


async def test_rotation_stands_down_while_the_lock_is_held(monkeypatch, druks_db):
    connection = await _seed_claude(
        access="old", refresh="R0", expires_at=_NOW + timedelta(minutes=30)
    )
    calls = _mock_post(
        monkeypatch, _resp(200, {"access_token": "new", "refresh_token": "R1", "expires_in": 100})
    )
    # A second identity on a lineage another refresher is mid-flight on trips the
    # provider's reuse detection — a held lock means no provider call at all.
    await druks.redis.get_client().set(f"druks:harness:refresh:{connection.id}", "1", ex=60)
    result = await AnthropicProvider.rotate_token(connection.id, now=_NOW)
    assert result.action == "locked"
    assert calls == []


async def test_rotation_lock_is_released_after_refresh(monkeypatch, druks_db):
    connection = await _seed_claude(
        access="old", refresh="R0", expires_at=_NOW + timedelta(minutes=30)
    )
    _mock_post(
        monkeypatch, _resp(200, {"access_token": "new", "refresh_token": "R1", "expires_in": 100})
    )
    await AnthropicProvider.rotate_token(connection.id, now=_NOW)
    assert not await druks.redis.get_client().get(f"druks:harness:refresh:{connection.id}")


async def test_two_fetches_inside_the_margin_rotate_once_and_read_the_same_token(
    monkeypatch, druks_db
):
    # Two boxes fetch at once inside the margin: the lock elects one identity, and
    # the second fetch reloads and answers with the token the first one stored.
    connection = await _seed_claude(
        access="old", refresh="R0", expires_at=_NOW + timedelta(minutes=30)
    )
    calls = _mock_post(
        monkeypatch,
        _resp(200, {"access_token": "new", "refresh_token": "R1", "expires_in": 28800}),
    )
    first = await AnthropicProvider.rotate_token(connection.id, now=_NOW)
    second = await AnthropicProvider.rotate_token(connection.id, now=_NOW)
    assert (first.action, second.action) == ("refreshed", "fresh")
    assert len(calls) == 1
    token = AnthropicProvider.load_token(await VaultSecret.reload(connection.id), now=_NOW)
    assert token.access_token == "new"
    assert token.expires_at == second.expires_at == _NOW + timedelta(seconds=28800)


async def test_a_failed_refresh_keeps_a_live_token_to_serve(monkeypatch, druks_db):
    # The issuer answers a box with the stored token while it is valid. A refresh
    # that fails inside the margin changes nothing the box can see.
    soon = _NOW + timedelta(minutes=30)
    connection = await _seed_claude(access="old", refresh="R0", expires_at=soon)
    _mock_post(monkeypatch, httpx.ConnectError("boom"))
    result = await AnthropicProvider.rotate_token(connection.id, now=_NOW)
    assert (result.action, result.error) == ("failed", "network")
    token = AnthropicProvider.load_token(await VaultSecret.reload(connection.id), now=_NOW)
    assert (token.access_token, token.expires_at) == ("old", soon)


async def test_a_failed_refresh_of_an_expired_token_leaves_nothing_to_serve(monkeypatch, druks_db):
    # Nothing valid is left, so the issuer answers 503 and the exchange retries.
    connection = await _seed_claude(
        access="old", refresh="R0", expires_at=_NOW - timedelta(minutes=1)
    )
    _mock_post(monkeypatch, httpx.ConnectError("boom"))
    await AnthropicProvider.rotate_token(connection.id, now=_NOW)
    with pytest.raises(OAuthTokenError) as error:
        AnthropicProvider.load_token(await VaultSecret.reload(connection.id), now=_NOW)
    assert error.value.tag == "token_expired"


async def test_disconnect_removes_only_the_addressed_login(druks_db):
    mine = await _seed_claude(provider_email="a@example.com")
    other = await _seed_claude(provider_email="b@example.com")

    await mine.revoke("user")

    assert await VaultSecret.reload(other.id)
    # The fallback account (the first) has no anthropic subscription left; another
    # account's subscription never leaks into execution.
    with pytest.raises(HarnessNotConnectedError):
        await AnthropicProvider.get_subscription(mine.account_id)


async def test_reconnect_restores_execution(druks_db):
    mine = await _seed_claude(provider_email="a@example.com")
    account_id = mine.account_id
    await mine.revoke("user")
    with pytest.raises(HarnessNotConnectedError):
        await AnthropicProvider.get_subscription(account_id)

    await _seed_claude(access="fresh", provider_email="a@example.com")
    restored = await AnthropicProvider.get_subscription(account_id)
    assert dict(restored.secrets)["claudeAiOauth"]["accessToken"] == "fresh"


async def test_connect_scopes_rows_by_provider_and_account(druks_db):
    claude_row = await _seed_claude(provider_email="a@example.com")
    codex_row = await _seed_codex(provider_email="a@example.com")
    other = await _seed_claude(provider_email="b@example.com")

    assert len({claude_row.id, codex_row.id, other.id}) == 3
    assert claude_row.account_id == codex_row.account_id  # same person, one account
    assert other.account_id != claude_row.account_id
    assert (await Account.get_for_username("a@example.com")).id == claude_row.account_id
    # The first account adopted the execution fallback.
    assert (await Account.get_default()).id == claude_row.account_id


async def test_reconnect_updates_the_existing_credential_in_place(druks_db):
    row = await _seed_claude(access="old", provider_email="a@example.com")
    # Same email, different case — citext matches it to the existing account,
    # so the reconnect updates that one connection rather than making a second.
    again = await _seed_claude(access="new", provider_email="A@Example.com")
    assert again.id == row.id
    assert dict(again.secrets)["claudeAiOauth"]["accessToken"] == "new"
    assert again.identity["email"] == "A@Example.com"  # stored as last given


async def test_claude_fetch_usage_success(monkeypatch, druks_db):
    connection = await _seed_claude(access="tok", expires_at=_NOW + timedelta(hours=2))
    body = {
        "five_hour": {"utilization": 16.0, "resets_at": "2026-06-04T23:19:59+00:00"},
        "seven_day": {"utilization": 48.0, "resets_at": "2026-06-07T16:00:00+00:00"},
    }
    calls = _mock_get(monkeypatch, _resp(200, body))
    parsed = await AnthropicProvider.fetch_usage(connection, now=_NOW)
    assert parsed.ok is True
    assert parsed.five_hour.percent_left == 84
    assert parsed.weeks[0].percent_left == 52
    assert calls[0]["headers"]["Authorization"] == "Bearer tok"
    assert calls[0]["headers"]["anthropic-beta"] == "oauth-2025-04-20"
    assert calls[0]["headers"]["User-Agent"].startswith("claude-code/")


async def test_claude_fetch_usage_http_error(monkeypatch, druks_db):
    connection = await _seed_claude(access="tok", expires_at=_NOW + timedelta(hours=2))
    _mock_get(monkeypatch, _resp(403, {"error": "x"}))
    parsed = await AnthropicProvider.fetch_usage(connection, now=_NOW)
    assert parsed.ok is False
    assert parsed.error == "forbidden_scope"


async def test_fetch_usage_without_a_token_skips_http(monkeypatch, druks_db):
    # The connection exists but its payload carries no access token — never fetch.
    connection = await connect_provider(AnthropicProvider, {"claudeAiOauth": {}})
    calls = _mock_get(monkeypatch, _resp(200, {}))
    parsed = await AnthropicProvider.fetch_usage(connection, now=_NOW)
    assert parsed.ok is False
    assert parsed.error == "no_token"
    assert calls == []  # no token => no request


async def test_codex_fetch_usage_success(monkeypatch, druks_db):
    connection = await _seed_codex(account_id="acc-7")
    body = {
        "plan_type": "pro",
        "rate_limit": {
            "primary_window": {
                "used_percent": 39,
                "limit_window_seconds": 18000,
                "reset_at": 1780625132,
            },
            "secondary_window": {
                "used_percent": 39,
                "limit_window_seconds": 604800,
                "reset_at": 1781211932,
            },
        },
    }
    calls = _mock_get(monkeypatch, _resp(200, body))
    parsed = await OpenAiProvider.fetch_usage(connection, now=_NOW)
    assert parsed.ok is True
    assert parsed.plan_tier == "pro"
    assert parsed.five_hour.percent_left == 61
    assert parsed.weeks[0].percent_left == 61
    assert calls[0]["headers"]["ChatGPT-Account-Id"] == "acc-7"


async def test_codex_usage_forces_a_refresh_on_401_then_retries(monkeypatch, druks_db):
    # The stored access token's JWT ``exp`` is days out, so the refresh loop
    # never touches it — but the provider 401s it server-side. fetch_usage must
    # trust the 401, refresh, and retry once.
    connection = await _seed_codex(account_id="acc-7")
    fresh = _jwt(int((_NOW + timedelta(days=9)).timestamp()))
    _mock_post(monkeypatch, _resp(200, {"access_token": fresh, "refresh_token": "R1"}))
    body = {
        "plan_type": "pro",
        "rate_limit": {
            "primary_window": {"used_percent": 39, "limit_window_seconds": 18000, "reset_at": 1},
            "secondary_window": {"used_percent": 39, "limit_window_seconds": 604800, "reset_at": 2},
        },
    }
    responses = [_resp(401, {"detail": {"code": "token_expired"}}), _resp(200, body)]
    gets = []

    async def fake_get(self, url, *, headers=None, **_kwargs):
        gets.append(headers)
        return responses[min(len(gets) - 1, len(responses) - 1)]

    monkeypatch.setattr(pbase.httpx.AsyncClient, "get", fake_get)
    parsed = await OpenAiProvider.fetch_usage(connection, now=_NOW)
    assert parsed.ok is True
    assert parsed.plan_tier == "pro"
    assert len(gets) == 2  # 401, then the post-refresh retry


async def test_codex_usage_reports_auth_required_when_the_refresh_is_revoked(monkeypatch, druks_db):
    # The subscription changed: both the access token and its refresh lineage
    # are revoked. The forced refresh gets invalid_grant, so we surface
    # auth_required rather than looping on the dead token.
    connection = await _seed_codex(account_id="acc-7")
    _mock_post(monkeypatch, _resp(400, {"error": "invalid_grant"}))
    _mock_get(monkeypatch, _resp(401, {"detail": {"code": "token_expired"}}))
    parsed = await OpenAiProvider.fetch_usage(connection, now=_NOW)
    assert parsed.ok is False
    assert parsed.error == "auth_required"


async def test_lookup_reads_only_the_accounts_own_subscription(druks_db):
    own = await _seed_claude(provider_email="a@example.com")
    other = await _seed_claude(provider_email="b@example.com")

    assert (await AnthropicProvider.get_subscription(own.account_id)).id == own.id
    assert (await AnthropicProvider.get_subscription(other.account_id)).id == other.id


async def test_lookup_never_falls_through_to_another_account_or_the_key(druks_db):
    # Another account's subscription and the installation's key both exist;
    # neither stands in, and the miss names the fix.
    await _seed_claude(provider_email="a@example.com")
    unsubscribed = await Account.get_or_create("b@example.com")
    await VaultSecret.paste(Audience.provider("anthropic"), "sk-shared", pasted_by=unsubscribed)

    with pytest.raises(HarnessNotConnectedError, match="connect your Anthropic subscription"):
        await AnthropicProvider.get_subscription(unsubscribed.id)
    with pytest.raises(HarnessNotConnectedError, match="connect your Anthropic subscription"):
        await AnthropicProvider.get_subscription(None)


async def test_a_providers_key_is_one_row_replaced_by_the_next_paste(druks_db):
    first = await Account.get_or_create("a@example.com")
    second = await Account.get_or_create("b@example.com")
    assert await VaultSecret.lookup(SecretKind.STATIC, Audience.provider("anthropic")) is None

    await VaultSecret.paste(Audience.provider("anthropic"), "sk-one", pasted_by=first)
    await VaultSecret.paste(Audience.provider("anthropic"), "sk-two", pasted_by=second)

    [stored] = await VaultSecret.list_keys()
    assert stored.secrets["value"] == "sk-two"
    assert stored.secrets["value"][-4:] == "-two"
    assert (await Account.get(stored.identity["pasted_by"])).username == "b@example.com"


async def test_minimal_provider_reports_unsupported_usage(monkeypatch):
    class MinimalProvider(pbase.Provider):
        id = "minimal"
        label = "Minimal"
        billing_options = frozenset({"api_key"})

    subscription = SimpleNamespace(secrets={})
    calls = _mock_get(monkeypatch, _resp(200, {}))

    assert await MinimalProvider.fetch_usage(subscription) == ParsedUsage(
        ok=False, error="unsupported"
    )
    assert calls == []


async def _bound_identity(subscription, *, host_id: str, run_id: str) -> SandboxIdentity:
    await seed_run(db_session(), kind=Summarize.kind, run_id=run_id)
    identity, _ = await SandboxIdentity.create(
        run_id=run_id,
        scoped_to="workflow",
        secret_refs=[SecretRef(name="anthropic", secret_id=subscription.id)],
    )
    await identity.bind(host_id)
    return identity


def _no_gate(subscription_id: str):
    raise AssertionError(f"a fresh token shut the gate of {subscription_id}")


_REFRESHED = {"access_token": "new", "refresh_token": "R1", "expires_in": 28800}
_REFRESH_URL = "http://127.0.0.1:8781/refresh/{host_id}/anthropic"


def _in(delta: timedelta) -> datetime:
    return datetime.now(UTC) + delta


async def test_a_fetch_answers_a_fresh_token_without_a_provider_call_or_the_gate(
    monkeypatch, druks_db
):
    connection = await _seed_claude(access="live", expires_at=_in(timedelta(hours=6)))
    calls = _mock_post(monkeypatch, _resp(200, _REFRESHED))
    monkeypatch.setattr(pbase.gate, "shut", _no_gate)

    token = await AnthropicProvider.issue_token(connection.id)

    assert token.access_token == "live"
    assert calls == []


async def test_two_fetches_inside_the_margin_rotate_once_request_once_and_answer_the_same_token(
    monkeypatch, druks_db
):
    connection = await _seed_claude(
        access="old", refresh="R0", expires_at=_in(timedelta(minutes=30))
    )
    await _bound_identity(connection, host_id="host-other", run_id="run-other")
    calls = _mock_post(monkeypatch, _resp(200, _REFRESHED))

    first = await AnthropicProvider.issue_token(connection.id, except_host_id="host-mine")
    second = await AnthropicProvider.issue_token(connection.id, except_host_id="host-mine")

    assert first.access_token == second.access_token == "new"
    assert [call["url"] for call in calls] == [
        AnthropicProvider._TOKEN_URL,
        _REFRESH_URL.format(host_id="host-other"),
    ]


async def test_a_fetch_on_a_busy_subscription_answers_the_current_token(monkeypatch, druks_db):
    # Inside the margin, above the call horizon: the call in flight keeps its token.
    connection = await _seed_claude(
        access="current", refresh="R0", expires_at=_in(timedelta(minutes=90))
    )
    calls = _mock_post(monkeypatch, _resp(200, _REFRESHED))

    async with gate.use(connection.id, "call-1"):
        token = await AnthropicProvider.issue_token(connection.id)

    assert token.access_token == "current"
    assert calls == []


async def test_a_fetch_rotates_a_busy_subscription_once_urgent(monkeypatch, druks_db):
    connection = await _seed_claude(
        access="old", refresh="R0", expires_at=_in(timedelta(minutes=30))
    )
    calls = _mock_post(monkeypatch, _resp(200, _REFRESHED))

    async with gate.use(connection.id, "call-1"):
        token = await AnthropicProvider.issue_token(connection.id)

    assert token.access_token == "new"
    assert calls[0]["json"]["refresh_token"] == "R0"


async def test_a_fetch_waits_out_a_shut_gate_then_answers_the_stored_token(monkeypatch, druks_db):
    monkeypatch.setattr(gate, "_POLL", 0.01)
    connection = await _seed_claude(
        access="old", refresh="R0", expires_at=_in(timedelta(minutes=30))
    )
    calls = _mock_post(monkeypatch, _resp(200, _REFRESHED))
    client = druks.redis.get_client()
    rotating = f"druks:sandbox:rotating:{connection.id}"
    await client.set(rotating, "1", ex=60)
    session = db_session()

    async def other_rotator() -> None:
        # The holder advances the row, then reopens the gate.
        await asyncio.sleep(0.03)
        await session.execute(
            update(VaultSecret)
            .where(VaultSecret.id == connection.id)
            .values(
                secrets=_claude_payload(
                    access="stored", refresh="R1", expires_at=_in(timedelta(hours=8))
                )
            )
        )
        await client.delete(rotating)

    holder = asyncio.create_task(other_rotator())
    token = await AnthropicProvider.issue_token(connection.id)
    await holder

    assert token.access_token == "stored"
    assert calls == []


async def test_a_rotation_requests_a_refresh_for_every_other_live_bound_identity(
    monkeypatch, druks_db
):
    connection = await _seed_claude(
        access="old", refresh="R0", expires_at=_in(timedelta(minutes=30))
    )
    await _bound_identity(connection, host_id="host-a", run_id="run-a")
    await _bound_identity(connection, host_id="host-mine", run_id="run-mine")
    await seed_run(db_session(), kind=Summarize.kind, run_id="run-unbound")
    await SandboxIdentity.create(
        run_id="run-unbound",
        scoped_to="workflow",
        secret_refs=[SecretRef(name="anthropic", secret_id=connection.id)],
    )
    revoked = await _bound_identity(connection, host_id="host-revoked", run_id="run-revoked")
    await revoked.revoke()
    other = await _seed_claude(
        access="x", refresh="RX", expires_at=_in(timedelta(hours=6)), provider_email="b@example.com"
    )
    await _bound_identity(other, host_id="host-elsewhere", run_id="run-elsewhere")
    calls = _mock_post(monkeypatch, _resp(200, _REFRESHED))

    result = await AnthropicProvider.rotate_token(connection.id, except_host_id="host-mine")

    assert result.action == "refreshed"
    assert [call["url"] for call in calls] == [
        AnthropicProvider._TOKEN_URL,
        _REFRESH_URL.format(host_id="host-a"),
    ]


async def test_a_failed_refresh_request_is_a_log_line_and_the_rotation_stands(
    monkeypatch, druks_db, caplog
):
    connection = await _seed_claude(
        access="old", refresh="R0", expires_at=_in(timedelta(minutes=30))
    )
    await _bound_identity(connection, host_id="host-a", run_id="run-a")

    async def fake_post(self, url, *, json=None, **_kwargs):
        if "/refresh/" in url:
            raise httpx.ConnectError("the exchange is down")
        return _resp(200, _REFRESHED)

    monkeypatch.setattr(pbase.httpx.AsyncClient, "post", fake_post)

    result = await AnthropicProvider.rotate_token(connection.id)

    assert result.action == "refreshed"
    assert (await _payload("anthropic"))["claudeAiOauth"]["accessToken"] == "new"
    assert "refresh request for box host-a service anthropic failed" in caplog.text


async def test_a_failed_rotation_answers_the_live_token(monkeypatch, druks_db):
    connection = await _seed_claude(
        access="old", refresh="R0", expires_at=_in(timedelta(minutes=30))
    )
    _mock_post(monkeypatch, httpx.ConnectError("boom"))

    token = await AnthropicProvider.issue_token(connection.id)

    assert token.access_token == "old"


async def test_an_expired_token_with_a_failed_rotation_answers_nothing(monkeypatch, druks_db):
    connection = await _seed_claude(
        access="old", refresh="R0", expires_at=_in(-timedelta(minutes=1))
    )
    _mock_post(monkeypatch, httpx.ConnectError("boom"))

    with pytest.raises(OAuthTokenError) as error:
        await AnthropicProvider.issue_token(connection.id)
    assert error.value.tag == "token_expired"


async def test_the_cron_requests_refreshes_after_a_rotation(monkeypatch, druks_db):
    connection = await _seed_claude(
        access="old", refresh="R0", expires_at=_in(timedelta(minutes=30))
    )
    await _bound_identity(connection, host_id="host-a", run_id="run-a")
    calls = _mock_post(monkeypatch, _resp(200, _REFRESHED))

    await tasks._refresh()

    assert [call["url"] for call in calls] == [
        AnthropicProvider._TOKEN_URL,
        _REFRESH_URL.format(host_id="host-a"),
    ]


async def test_the_usage_fetch_requests_refreshes_after_its_rotation(monkeypatch, druks_db):
    connection = await _seed_claude(access="dead", refresh="R0", expires_at=_in(timedelta(hours=6)))
    await _bound_identity(connection, host_id="host-a", run_id="run-a")
    posts = _mock_post(monkeypatch, _resp(200, _REFRESHED))
    usage = {
        "five_hour": {"utilization": 16.0, "resets_at": "2026-06-04T23:19:59+00:00"},
        "seven_day": {"utilization": 48.0, "resets_at": "2026-06-07T16:00:00+00:00"},
    }
    answers = [_resp(401, {"error": "revoked"}), _resp(200, usage)]

    async def fake_get(self, url, *, headers=None, **_kwargs):
        return answers.pop(0)

    monkeypatch.setattr(pbase.httpx.AsyncClient, "get", fake_get)

    parsed = await AnthropicProvider.fetch_usage(connection)

    assert parsed.ok
    assert [call["url"] for call in posts] == [
        AnthropicProvider._TOKEN_URL,
        _REFRESH_URL.format(host_id="host-a"),
    ]

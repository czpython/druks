import base64
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import druks.redis
import httpx
import pytest
from conftest import connect_provider
from druks.accounts.models import Account
from druks.database import db_session
from druks.harnesses import providers as pbase
from druks.harnesses.datastructures import ParsedUsage
from druks.harnesses.exceptions import HarnessNotConnectedError, OAuthTokenError
from druks.harnesses.models import ProviderKey, ProviderSubscription
from druks.harnesses.providers import AnthropicProvider, OpenAiProvider
from druks.user_settings.models import UserSettings

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


async def _seed_claude(*, provider_email="op@example.com", **kwargs) -> ProviderSubscription:
    return await connect_provider(
        AnthropicProvider, _claude_payload(**kwargs), provider_email=provider_email
    )


def _codex_payload(*, access=None, refresh="R0", account_id="acc-1", id_token="id-0") -> dict:
    access = access or _jwt(int((_NOW + timedelta(days=9)).timestamp()))
    tokens = {"access_token": access, "id_token": id_token, "account_id": account_id}
    if refresh is not None:
        tokens["refresh_token"] = refresh
    return {"auth_mode": "chatgpt", "OPENAI_API_KEY": None, "tokens": tokens}


async def _seed_codex(*, provider_email="op@example.com", **kwargs) -> ProviderSubscription:
    return await connect_provider(
        OpenAiProvider, _codex_payload(**kwargs), provider_email=provider_email
    )


async def _payload(provider_id: str) -> dict:
    # Rotation commits in its own session; refresh past this session's identity map.
    row = await ProviderSubscription.get_for_account(provider_id, fallback=True)
    await db_session().refresh(row)
    return row.payload


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
    assert not await ProviderSubscription.list_all()
    with pytest.raises(HarnessNotConnectedError):
        await ProviderSubscription.lookup("anthropic", account_id)


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
    # grant POST.
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
        dict((await ProviderSubscription.get(stale_id)).payload)["claudeAiOauth"]["accessToken"]
        == "new"
    )
    assert (
        dict((await ProviderSubscription.get(other_id)).payload)["claudeAiOauth"]["accessToken"]
        == "keep"
    )


async def test_invalid_grant_drops_only_the_addressed_row(monkeypatch, druks_db):
    kept = await _seed_claude(access="d", expires_at=_NOW - timedelta(minutes=1))
    other = await _seed_claude(
        access="o", expires_at=_NOW - timedelta(minutes=1), provider_email="b@example.com"
    )
    kept_id, other_id = kept.id, other.id
    _mock_post(monkeypatch, _resp(400, {"error": "invalid_grant"}))
    await AnthropicProvider.rotate_token(other_id, now=_NOW)
    assert not await ProviderSubscription.get(other_id)
    assert await ProviderSubscription.get(kept_id)


async def test_rotation_stands_down_while_the_lock_is_held(monkeypatch, druks_db):
    connection = await _seed_claude(
        access="old", refresh="R0", expires_at=_NOW + timedelta(minutes=30)
    )
    calls = _mock_post(
        monkeypatch, _resp(200, {"access_token": "new", "refresh_token": "R1", "expires_in": 100})
    )
    # A second grant on a lineage another refresher is mid-flight on trips the
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


async def test_disconnect_removes_only_the_addressed_login(druks_db):
    mine = await _seed_claude(provider_email="a@example.com")
    other = await _seed_claude(provider_email="b@example.com")

    await mine.delete()

    assert await ProviderSubscription.get(other.id)
    # The fallback account (the first) has no anthropic subscription left; another
    # account's subscription never leaks into execution.
    with pytest.raises(HarnessNotConnectedError):
        await ProviderSubscription.lookup("anthropic", mine.account_id)


async def test_reconnect_restores_execution(druks_db):
    mine = await _seed_claude(provider_email="a@example.com")
    account_id = mine.account_id
    await mine.delete()
    with pytest.raises(HarnessNotConnectedError):
        await ProviderSubscription.lookup("anthropic", account_id)

    await _seed_claude(access="fresh", provider_email="a@example.com")
    restored = await ProviderSubscription.lookup("anthropic", account_id)
    assert dict(restored.payload)["claudeAiOauth"]["accessToken"] == "fresh"


async def test_connect_scopes_rows_by_provider_and_account(druks_db):
    claude_row = await _seed_claude(provider_email="a@example.com")
    codex_row = await _seed_codex(provider_email="a@example.com")
    other = await _seed_claude(provider_email="b@example.com")

    assert len({claude_row.id, codex_row.id, other.id}) == 3
    assert claude_row.account_id == codex_row.account_id  # same person, one account
    assert other.account_id != claude_row.account_id
    assert (await Account.get_for_username("a@example.com")).id == claude_row.account_id
    # The first account adopted the execution fallback.
    assert (await UserSettings.get()).fallback_account_id == claude_row.account_id


async def test_reconnect_updates_the_existing_credential_in_place(druks_db):
    row = await _seed_claude(access="old", provider_email="a@example.com")
    # Same email, different case — citext matches it to the existing account,
    # so the reconnect updates that one connection rather than making a second.
    again = await _seed_claude(access="new", provider_email="A@Example.com")
    assert again.id == row.id
    assert dict(again.payload)["claudeAiOauth"]["accessToken"] == "new"
    assert again.provider_email == "A@Example.com"  # stored as last given


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


async def test_lookup_reads_only_the_accounts_own_subscription(druks_db):
    own = await _seed_claude(provider_email="a@example.com")
    other = await _seed_claude(provider_email="b@example.com")

    assert (await ProviderSubscription.lookup("anthropic", own.account_id)).id == own.id
    assert (await ProviderSubscription.lookup("anthropic", other.account_id)).id == other.id


async def test_lookup_never_falls_through_to_another_account_or_the_key(druks_db):
    # Another account's subscription and the installation's key both exist;
    # neither stands in, and the miss names the fix.
    await _seed_claude(provider_email="a@example.com")
    unsubscribed = await Account.get_or_create("b@example.com")
    await ProviderKey.create(provider="anthropic", key="sk-shared", account=unsubscribed)

    with pytest.raises(HarnessNotConnectedError, match="connect your Anthropic subscription"):
        await ProviderSubscription.lookup("anthropic", unsubscribed.id)
    with pytest.raises(HarnessNotConnectedError, match="connect your Anthropic subscription"):
        await ProviderSubscription.lookup("anthropic", None)


async def test_a_providers_key_is_one_row_replaced_by_the_next_paste(druks_db):
    first = await Account.get_or_create("a@example.com")
    second = await Account.get_or_create("b@example.com")
    assert await ProviderKey.get("anthropic") is None

    await ProviderKey.create(provider="anthropic", key="sk-one", account=first)
    await ProviderKey.create(provider="anthropic", key="sk-two", account=second)

    [stored] = await ProviderKey.list_all()
    assert stored.value.decrypt() == "sk-two"
    assert stored.key_tail == "-two"
    assert stored.updated_by.username == "b@example.com"


async def test_minimal_provider_reports_unsupported_usage(monkeypatch):
    class MinimalProvider(pbase.Provider):
        id = "minimal"
        label = "Minimal"
        login_kinds = frozenset({"api_key"})

    subscription = SimpleNamespace(payload={})
    calls = _mock_get(monkeypatch, _resp(200, {}))

    assert await MinimalProvider.fetch_usage(subscription) == ParsedUsage(
        ok=False, error="unsupported"
    )
    assert calls == []

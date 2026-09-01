import base64
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import druks.redis
import httpx
import pytest
from conftest import connect_harness
from druks.accounts.models import Account
from druks.harnesses import base as hbase
from druks.harnesses.claude import ClaudeHarness, _get_credentials
from druks.harnesses.codex import CodexHarness
from druks.harnesses.datastructures import (
    AgentInvocation,
    HarnessRunResult,
    ParsedModels,
    ParsedUsage,
    SandboxSettings,
)
from druks.harnesses.exceptions import HarnessNotConnectedError, OAuthTokenError
from druks.harnesses.models import HarnessConnection
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


async def _seed_claude(*, provider_email="op@example.com", **kwargs) -> HarnessConnection:
    return await connect_harness(
        ClaudeHarness, _claude_payload(**kwargs), provider_email=provider_email
    )


def _codex_payload(*, access=None, refresh="R0", account_id="acc-1", id_token="id-0") -> dict:
    access = access or _jwt(int((_NOW + timedelta(days=9)).timestamp()))
    tokens = {"access_token": access, "id_token": id_token, "account_id": account_id}
    if refresh is not None:
        tokens["refresh_token"] = refresh
    return {"auth_mode": "chatgpt", "OPENAI_API_KEY": None, "tokens": tokens}


async def _seed_codex(*, provider_email="op@example.com", **kwargs) -> HarnessConnection:
    return await connect_harness(
        CodexHarness, _codex_payload(**kwargs), provider_email=provider_email
    )


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

    monkeypatch.setattr(hbase.httpx.AsyncClient, "post", fake_post)
    return calls


def _mock_get(monkeypatch, response):
    calls = []

    async def fake_get(self, url, *, headers=None, **_kwargs):
        calls.append({"url": url, "headers": headers})
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr(hbase.httpx.AsyncClient, "get", fake_get)
    return calls


async def test_claude_load_token(druks_db):
    connection = await _seed_claude(access="live", expires_at=_NOW + timedelta(hours=2))
    token = ClaudeHarness.load_token(connection, now=_NOW)
    assert token.access_token == "live"
    assert token.subscription_type == "max"
    assert "user:profile" in token.scopes


async def test_claude_load_token_expired(druks_db):
    connection = await _seed_claude(expires_at=_NOW - timedelta(hours=1))
    with pytest.raises(OAuthTokenError) as e:
        ClaudeHarness.load_token(connection, now=_NOW)
    assert e.value.tag == "token_expired"


async def test_claude_load_token_no_access(druks_db):
    connection = await connect_harness(
        ClaudeHarness, {"claudeAiOauth": {"subscriptionType": "max"}}
    )
    with pytest.raises(OAuthTokenError) as e:
        ClaudeHarness.load_token(connection, now=_NOW)
    assert e.value.tag == "no_token"


async def test_codex_load_token(druks_db):
    connection = await _seed_codex()
    token = CodexHarness.load_token(connection, now=_NOW)
    assert "." in token.access_token
    assert token.account_id == "acc-1"


async def test_codex_load_token_expired(druks_db):
    connection = await _seed_codex(access=_jwt(int((_NOW - timedelta(hours=1)).timestamp())))
    with pytest.raises(OAuthTokenError) as e:
        CodexHarness.load_token(connection, now=_NOW)
    assert e.value.tag == "token_expired"


async def test_claude_fresh_not_refreshed(monkeypatch, druks_db):
    connection = await _seed_claude(expires_at=_NOW + timedelta(hours=6))
    calls = _mock_post(monkeypatch, _resp(200, {}))
    result = await ClaudeHarness.rotate_token(connection.id, now=_NOW)
    assert result.action == "fresh"
    assert result.connection_id == connection.id
    assert calls == []


async def test_claude_stale_refreshes_and_persists(monkeypatch, druks_db):
    soon = _NOW + timedelta(minutes=30)
    connection = await _seed_claude(access="old", refresh="R0", expires_at=soon)
    calls = _mock_post(
        monkeypatch, _resp(200, {"access_token": "new", "refresh_token": "R1", "expires_in": 28800})
    )
    result = await ClaudeHarness.rotate_token(connection.id, now=_NOW)
    assert result.action == "refreshed"
    assert calls[0]["json"]["refresh_token"] == "R0"
    block = (await ClaudeHarness.get_credentials())["claudeAiOauth"]
    assert block["accessToken"] == "new"
    assert block["refreshToken"] == "R1"
    assert block["scopes"] == ["user:profile"]  # preserved
    assert block["subscriptionType"] == "max"  # preserved
    assert block["expiresAt"] == int((_NOW + timedelta(seconds=28800)).timestamp() * 1000)


async def test_claude_invalid_grant_drops_row(monkeypatch, druks_db):
    connection = await _seed_claude(access="old", expires_at=_NOW - timedelta(minutes=1))
    _mock_post(monkeypatch, _resp(400, {"error": "invalid_grant"}))
    result = await ClaudeHarness.rotate_token(connection.id, now=_NOW)
    assert result.action == "failed"
    assert result.error == "invalid_grant"
    # A revoked lineage self-disconnects and commits inside the rotation — the
    # deletion never rides (or rolls back with) the tick's later commit.
    assert not await HarnessConnection.list_all()
    with pytest.raises(HarnessNotConnectedError):
        await ClaudeHarness.get_credentials()


async def test_claude_network_error_keeps_row(monkeypatch, druks_db):
    connection = await _seed_claude(access="old", expires_at=_NOW - timedelta(minutes=1))
    _mock_post(monkeypatch, httpx.ConnectError("boom"))
    result = await ClaudeHarness.rotate_token(connection.id, now=_NOW)
    assert result.error == "network"
    assert (await ClaudeHarness.get_credentials())["claudeAiOauth"]["accessToken"] == "old"


async def test_claude_http_500_keeps_row(monkeypatch, druks_db):
    connection = await _seed_claude(access="old", expires_at=_NOW - timedelta(minutes=1))
    _mock_post(monkeypatch, _resp(500, ""))
    result = await ClaudeHarness.rotate_token(connection.id, now=_NOW)
    assert result.error == "http_500"
    assert (await ClaudeHarness.get_credentials())["claudeAiOauth"]["accessToken"] == "old"


async def test_claude_bad_response_keeps_row(monkeypatch, druks_db):
    connection = await _seed_claude(access="old", expires_at=_NOW - timedelta(minutes=1))
    _mock_post(monkeypatch, _resp(200, "not json"))
    result = await ClaudeHarness.rotate_token(connection.id, now=_NOW)
    assert result.error == "bad_response"
    assert (await ClaudeHarness.get_credentials())["claudeAiOauth"]["accessToken"] == "old"


async def test_rotation_of_a_deleted_row_is_a_no_op(monkeypatch, druks_db):
    connection = await _seed_claude(access="old", expires_at=_NOW - timedelta(minutes=1))
    connection_id = connection.id
    _mock_post(monkeypatch, _resp(400, {"error": "invalid_grant"}))
    await ClaudeHarness.rotate_token(connection_id, now=_NOW)
    # Row is gone; rotating the stale id must short-circuit before any
    # grant POST.
    calls = _mock_post(monkeypatch, _resp(200, {"access_token": "x"}))
    result = await ClaudeHarness.rotate_token(connection_id, now=_NOW)
    assert result.action == "failed"
    assert result.error == "no_credentials"
    assert calls == []


async def test_claude_relogin_overwrite_picked_up(monkeypatch, druks_db):
    connection = await _seed_claude(refresh="R_NEW", expires_at=_NOW - timedelta(minutes=1))
    calls = _mock_post(
        monkeypatch, _resp(200, {"access_token": "a", "refresh_token": "b", "expires_in": 100})
    )
    await ClaudeHarness.rotate_token(connection.id, now=_NOW)
    assert calls[0]["json"]["refresh_token"] == "R_NEW"


async def test_codex_stale_refreshes_and_preserves(monkeypatch, druks_db):
    stale = _jwt(int((_NOW + timedelta(hours=1)).timestamp()))
    fresh = _jwt(int((_NOW + timedelta(days=10)).timestamp()))
    connection = await _seed_codex(access=stale, refresh="R0", account_id="acc-9")
    calls = _mock_post(
        monkeypatch, _resp(200, {"access_token": fresh, "refresh_token": "R1", "id_token": "id-1"})
    )
    result = await CodexHarness.rotate_token(connection.id, now=_NOW)
    assert result.action == "refreshed"
    assert calls[0]["json"]["client_id"] == "app_EMoamEEZ73f0CkXaXp7hrann"
    data = await CodexHarness.get_credentials()
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
    await CodexHarness.rotate_token(connection.id, now=_NOW)
    assert (await CodexHarness.get_credentials())["tokens"]["refresh_token"] == "KEEP"


async def test_codex_no_refresh_token(monkeypatch, druks_db):
    stale = _jwt(int((_NOW + timedelta(hours=1)).timestamp()))
    connection = await _seed_codex(refresh=None, access=stale)
    calls = _mock_post(monkeypatch, _resp(200, {}))
    result = await CodexHarness.rotate_token(connection.id, now=_NOW)
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
    result = await ClaudeHarness.rotate_token(stale_id, now=_NOW)
    assert result.action == "refreshed"
    assert (
        dict((await HarnessConnection.get(stale_id)).payload)["claudeAiOauth"]["accessToken"]
        == "new"
    )
    assert (
        dict((await HarnessConnection.get(other_id)).payload)["claudeAiOauth"]["accessToken"]
        == "keep"
    )


async def test_invalid_grant_drops_only_the_addressed_row(monkeypatch, druks_db):
    kept = await _seed_claude(access="d", expires_at=_NOW - timedelta(minutes=1))
    other = await _seed_claude(
        access="o", expires_at=_NOW - timedelta(minutes=1), provider_email="b@example.com"
    )
    kept_id, other_id = kept.id, other.id
    _mock_post(monkeypatch, _resp(400, {"error": "invalid_grant"}))
    await ClaudeHarness.rotate_token(other_id, now=_NOW)
    assert not await HarnessConnection.get(other_id)
    assert await HarnessConnection.get(kept_id)


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
    result = await ClaudeHarness.rotate_token(connection.id, now=_NOW)
    assert result.action == "locked"
    assert calls == []


async def test_rotation_lock_is_released_after_refresh(monkeypatch, druks_db):
    connection = await _seed_claude(
        access="old", refresh="R0", expires_at=_NOW + timedelta(minutes=30)
    )
    _mock_post(
        monkeypatch, _resp(200, {"access_token": "new", "refresh_token": "R1", "expires_in": 100})
    )
    await ClaudeHarness.rotate_token(connection.id, now=_NOW)
    assert not await druks.redis.get_client().get(f"druks:harness:refresh:{connection.id}")


async def test_disconnect_removes_only_the_addressed_login(druks_db):
    mine = await _seed_claude(provider_email="a@example.com")
    other = await _seed_claude(provider_email="b@example.com")

    await mine.delete()

    assert await HarnessConnection.get(other.id)
    # The fallback account (the first) has no claude connection left; another
    # account's credential never leaks into execution.
    with pytest.raises(HarnessNotConnectedError):
        await ClaudeHarness.get_credentials()


async def test_reconnect_restores_execution(druks_db):
    mine = await _seed_claude(provider_email="a@example.com")
    await mine.delete()
    with pytest.raises(HarnessNotConnectedError):
        await ClaudeHarness.get_credentials()

    await _seed_claude(access="fresh", provider_email="a@example.com")
    assert (await ClaudeHarness.get_credentials())["claudeAiOauth"]["accessToken"] == "fresh"


async def test_connect_scopes_rows_by_harness_and_account(druks_db):
    claude_row = await _seed_claude(provider_email="a@example.com")
    codex_row = await _seed_codex(provider_email="a@example.com")
    other = await _seed_claude(provider_email="b@example.com")

    assert len({claude_row.id, codex_row.id, other.id}) == 3
    assert claude_row.account_id == codex_row.account_id  # same person, one account
    assert other.account_id != claude_row.account_id
    assert (await Account.get_for_username("a@example.com")).id == claude_row.account_id
    # The first account adopted the execution fallback.
    assert (await UserSettings.get()).fallback_account_id == claude_row.account_id


async def test_reconnect_updates_the_existing_login_in_place(druks_db):
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
    parsed = await ClaudeHarness.fetch_usage(connection, now=_NOW)
    assert parsed.ok is True
    assert parsed.five_hour.percent_left == 84
    assert parsed.weeks[0].percent_left == 52
    assert calls[0]["headers"]["Authorization"] == "Bearer tok"
    assert calls[0]["headers"]["anthropic-beta"] == "oauth-2025-04-20"
    assert calls[0]["headers"]["User-Agent"].startswith("claude-code/")


async def test_claude_fetch_usage_http_error(monkeypatch, druks_db):
    connection = await _seed_claude(access="tok", expires_at=_NOW + timedelta(hours=2))
    _mock_get(monkeypatch, _resp(403, {"error": "x"}))
    parsed = await ClaudeHarness.fetch_usage(connection, now=_NOW)
    assert parsed.ok is False
    assert parsed.error == "forbidden_scope"


async def test_fetch_usage_without_a_token_skips_http(monkeypatch, druks_db):
    # The connection exists but its payload carries no access token — never fetch.
    connection = await connect_harness(ClaudeHarness, {"claudeAiOauth": {}})
    calls = _mock_get(monkeypatch, _resp(200, {}))
    parsed = await ClaudeHarness.fetch_usage(connection, now=_NOW)
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
    parsed = await CodexHarness.fetch_usage(connection, now=_NOW)
    assert parsed.ok is True
    assert parsed.plan_tier == "pro"
    assert parsed.five_hour.percent_left == 61
    assert parsed.weeks[0].percent_left == 61
    assert calls[0]["headers"]["ChatGPT-Account-Id"] == "acc-7"


async def test_claude_fetch_models_success(monkeypatch, druks_db):
    # fetch_models reads the wall clock (no ``now=`` seam), so the token's
    # expiry must be real-future, not _NOW-relative.
    connection = await _seed_claude(access="tok", expires_at=datetime.now(UTC) + timedelta(hours=2))
    body = {"data": [{"id": "claude-fable-5", "display_name": "Claude Fable 5"}]}
    calls = _mock_get(monkeypatch, _resp(200, body))

    parsed = await ClaudeHarness.fetch_models(connection)

    assert parsed == ParsedModels(
        ok=True,
        models=({"id": "claude-fable-5", "label": "Claude Fable 5"},),
        raw=json.dumps(body),
    )
    assert calls[0]["url"] == "https://api.anthropic.com/v1/models?limit=100"
    assert calls[0]["headers"]["Authorization"] == "Bearer tok"


async def test_codex_fetch_models_success(monkeypatch, druks_db):
    connection = await _seed_codex(
        account_id="acc-7",
        access=_jwt(int((datetime.now(UTC) + timedelta(days=9)).timestamp())),
    )
    body = {
        "models": [
            {
                "slug": "gpt-5.6-sol",
                "display_name": "GPT-5.6-Sol",
                "visibility": "list",
                "supported_reasoning_levels": [{"effort": "high"}],
                "minimal_client_version": "0.144.0",
            }
        ]
    }
    calls = _mock_get(monkeypatch, _resp(200, body))

    parsed = await CodexHarness.fetch_models(connection)

    assert parsed == ParsedModels(
        ok=True,
        models=(
            {
                "id": "gpt-5.6-sol",
                "label": "GPT-5.6-Sol",
                "efforts": ["high"],
                "minimal_client_version": "0.144.0",
            },
        ),
        raw=json.dumps(body),
    )
    assert calls[0]["url"] == (
        "https://chatgpt.com/backend-api/codex/models?client_version=99.99.99"
    )
    assert calls[0]["headers"]["ChatGPT-Account-Id"] == "acc-7"


async def test_render_credentials_file_serializes_stored_payload(druks_db):
    connection = await _seed_claude(access="tok", refresh="R0")
    rendered = await ClaudeHarness.render_credentials_file(connection.id)
    assert json.loads(rendered)["claudeAiOauth"]["accessToken"] == "tok"


async def test_render_credentials_file_raises_when_not_connected(druks_db):
    # No selection and no fallback connection at all.
    with pytest.raises(HarnessNotConnectedError, match="connect it in Settings"):
        await ClaudeHarness.render_credentials_file()


async def test_claude_builder_puts_db_credentials_on_the_bundle(druks_db):
    await _seed_claude(access="live", refresh="R0")
    sandbox = SandboxSettings(
        service_url="x",
        service_token="x",
        service_timeout=30.0,
        image="x",
        claude_config_dir=Path("/home/agent/.claude"),
        codex_config_dir=Path("/home/agent/.codex"),
    )
    bundle = await _get_credentials(sandbox, github_token=None)
    [(credential_path, rendered_content)] = bundle.files
    assert credential_path == ".claude/.credentials.json"
    assert json.loads(rendered_content)["claudeAiOauth"]["accessToken"] == "live"


async def test_credentials_builders_carry_global_instructions(druks_db):
    await _seed_claude()
    await _seed_codex()
    claude_config_dir = Path("/home/agent/.claude")
    codex_config_dir = Path("/home/agent/.codex")
    sandbox = SandboxSettings(
        service_url="x",
        service_token="x",
        service_timeout=30.0,
        image="x",
        claude_config_dir=claude_config_dir,
        codex_config_dir=codex_config_dir,
    )

    claude_bundle = await _get_credentials(sandbox, github_token=None)
    codex_bundle = await CodexHarness(
        model=CodexHarness.default_model,
        fast_mode=False,
        effort=None,
        sandbox=sandbox,
    )._get_credentials(github_token=None)

    assert claude_bundle.files[0][0] == ".claude/.credentials.json"
    assert codex_bundle.files[0][0] == ".codex/auth.json"
    assert (
        claude_config_dir / "CLAUDE.md",
        ".claude/CLAUDE.md",
    ) in claude_bundle.extra_config_files
    assert (
        codex_config_dir / "AGENTS.md",
        ".codex/AGENTS.md",
    ) in codex_bundle.extra_config_files


async def test_no_config_dir_ships_credential_only(druks_db):
    # No local config dir for the CLI => nothing of the host's config/plugins
    # reaches the sandbox — but the DB credential still ships: connection state
    # alone decides whether a harness can run.
    await _seed_claude(access="live")
    sandbox = SandboxSettings(
        service_url="x",
        service_token="x",
        service_timeout=30.0,
        image="x",
        claude_config_dir=None,
        codex_config_dir=None,
    )
    bundle = await _get_credentials(sandbox, github_token="gh")
    [(_, rendered_content)] = bundle.files
    assert json.loads(rendered_content)["claudeAiOauth"]["accessToken"] == "live"
    assert bundle.extra_config_files == ()
    assert bundle.extra_config_dirs == ()
    assert bundle.github_token == "gh"


async def test_claude_builder_raises_when_not_connected(druks_db):
    sandbox = SandboxSettings(
        service_url="x",
        service_token="x",
        service_timeout=30.0,
        image="x",
        claude_config_dir=Path("/home/agent/.claude"),
        codex_config_dir=None,
    )
    with pytest.raises(HarnessNotConnectedError, match="claude is not connected"):
        await _get_credentials(sandbox, github_token=None)


async def test_lookup_prefers_the_accounts_own_connection(druks_db):
    fallback = await _seed_claude(provider_email="a@example.com")  # a@ adopts the fallback
    own = await _seed_claude(provider_email="b@example.com")

    assert (await HarnessConnection.lookup("claude", own.account_id)).id == own.id
    assert (await HarnessConnection.lookup("claude", fallback.account_id)).id == fallback.id


async def test_lookup_falls_back(druks_db):
    fallback = await _seed_claude(provider_email="a@example.com")
    codex_only = await _seed_codex(provider_email="b@example.com")

    # An account with no claude connection, and no account at all.
    assert (await HarnessConnection.lookup("claude", codex_only.account_id)).id == fallback.id
    assert (await HarnessConnection.lookup("claude", None)).id == fallback.id


async def test_lookup_without_any_connection_raises(druks_db):
    await _seed_codex(provider_email="a@example.com")  # the fallback account has codex only
    with pytest.raises(HarnessNotConnectedError, match="connect it in Settings"):
        await HarnessConnection.lookup("claude", None)


async def test_render_credentials_file_renders_only_the_selected_login(druks_db):
    mine = await _seed_claude(access="mine-token", provider_email="a@example.com")
    other = await _seed_claude(access="other-token", provider_email="b@example.com")

    rendered = json.loads(await ClaudeHarness.render_credentials_file(other.id))
    assert rendered["claudeAiOauth"]["accessToken"] == "other-token"
    assert "mine-token" not in json.dumps(rendered)
    rendered = json.loads(await ClaudeHarness.render_credentials_file(mine.id))
    assert rendered["claudeAiOauth"]["accessToken"] == "mine-token"


async def test_render_credentials_file_for_a_deleted_connection_raises(druks_db):
    await _seed_claude(provider_email="a@example.com")  # the surviving fallback
    gone = await _seed_claude(provider_email="b@example.com")
    gone_id = gone.id
    await gone.delete()
    # A disconnect between selection and render fails the call — it must never
    # fall through to another account's payload.
    with pytest.raises(HarnessNotConnectedError, match="removed"):
        await ClaudeHarness.render_credentials_file(gone_id)


async def test_minimal_harness_reports_unsupported_optional_endpoints(monkeypatch):
    class MinimalHarness(hbase.Harness):
        name = "minimal"

        async def build_invocation(self, **kwargs: object) -> AgentInvocation:
            raise AssertionError("not called")

        def parse(
            self,
            result: HarnessRunResult,
            *,
            artifact_dir: Path,
            run_id: str,
        ) -> object:
            return {}

    harness = MinimalHarness(model="minimal", fast_mode=False, effort=None)
    connection = SimpleNamespace(payload={})
    calls = _mock_get(monkeypatch, _resp(200, {}))

    usage = await harness.fetch_usage(connection)
    models = await harness.fetch_models(connection)

    assert usage == ParsedUsage(ok=False, error="unsupported")
    assert models == ParsedModels(ok=False, error="unsupported")
    assert calls == []

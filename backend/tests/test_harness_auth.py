import asyncio
import base64
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from conftest import connect_harness
from druks.accounts.models import Account
from druks.harnesses import base as hbase
from druks.harnesses.claude import ClaudeHarness
from druks.harnesses.codex import CodexHarness
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


def _seed_claude(*, provider_email="op@example.com", **kwargs) -> HarnessConnection:
    return connect_harness(ClaudeHarness, _claude_payload(**kwargs), provider_email=provider_email)


def _codex_payload(*, access=None, refresh="R0", account_id="acc-1", id_token="id-0") -> dict:
    access = access or _jwt(int((_NOW + timedelta(days=9)).timestamp()))
    tokens = {"access_token": access, "id_token": id_token, "account_id": account_id}
    if refresh is not None:
        tokens["refresh_token"] = refresh
    return {"auth_mode": "chatgpt", "OPENAI_API_KEY": None, "tokens": tokens}


def _seed_codex(*, provider_email="op@example.com", **kwargs) -> HarnessConnection:
    return connect_harness(CodexHarness, _codex_payload(**kwargs), provider_email=provider_email)


def _resp(status: int, body: object) -> httpx.Response:
    text = body if isinstance(body, str) else json.dumps(body)
    return httpx.Response(status, text=text, request=httpx.Request("GET", "https://x"))


def _mock_post(monkeypatch, response):
    calls = []

    async def fake_post(self, url, *, json=None, **_kwargs):
        calls.append({"url": url, "json": json})
        # Yield to the event loop so a concurrent rotation attempt can run
        # while this grant is "in flight" — the shape the lock exists for.
        await asyncio.sleep(0)
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


def test_claude_load_token(db_session):
    _seed_claude(access="live", expires_at=_NOW + timedelta(hours=2))
    token = ClaudeHarness.load_token(now=_NOW)
    assert token.access_token == "live"
    assert token.subscription_type == "max"
    assert "user:profile" in token.scopes


def test_claude_load_token_expired(db_session):
    _seed_claude(expires_at=_NOW - timedelta(hours=1))
    with pytest.raises(OAuthTokenError) as e:
        ClaudeHarness.load_token(now=_NOW)
    assert e.value.tag == "token_expired"


def test_claude_load_token_missing(db_session):
    # No row => not connected.
    with pytest.raises(OAuthTokenError) as e:
        ClaudeHarness.load_token(now=_NOW)
    assert e.value.tag == "no_credentials"


def test_claude_load_token_no_access(db_session):
    connect_harness(ClaudeHarness, {"claudeAiOauth": {"subscriptionType": "max"}})
    with pytest.raises(OAuthTokenError) as e:
        ClaudeHarness.load_token(now=_NOW)
    assert e.value.tag == "no_token"


def test_codex_load_token(db_session):
    _seed_codex()
    token = CodexHarness.load_token(now=_NOW)
    assert "." in token.access_token
    assert token.account_id == "acc-1"


def test_codex_load_token_expired(db_session):
    _seed_codex(access=_jwt(int((_NOW - timedelta(hours=1)).timestamp())))
    with pytest.raises(OAuthTokenError) as e:
        CodexHarness.load_token(now=_NOW)
    assert e.value.tag == "token_expired"


async def test_claude_fresh_not_refreshed(monkeypatch, db_session):
    login = _seed_claude(expires_at=_NOW + timedelta(hours=6))
    calls = _mock_post(monkeypatch, _resp(200, {}))
    result = await ClaudeHarness.rotate_token(login.id, now=_NOW)
    assert result.action == "fresh"
    assert result.login_id == login.id
    assert calls == []


async def test_claude_stale_refreshes_and_persists(monkeypatch, db_session):
    soon = _NOW + timedelta(minutes=30)
    login = _seed_claude(access="old", refresh="R0", expires_at=soon)
    calls = _mock_post(
        monkeypatch, _resp(200, {"access_token": "new", "refresh_token": "R1", "expires_in": 28800})
    )
    result = await ClaudeHarness.rotate_token(login.id, now=_NOW)
    assert result.action == "refreshed"
    assert calls[0]["json"]["refresh_token"] == "R0"
    block = ClaudeHarness.get_credentials()["claudeAiOauth"]
    assert block["accessToken"] == "new"
    assert block["refreshToken"] == "R1"
    assert block["scopes"] == ["user:profile"]  # preserved
    assert block["subscriptionType"] == "max"  # preserved
    assert block["expiresAt"] == int((_NOW + timedelta(seconds=28800)).timestamp() * 1000)


async def test_claude_invalid_grant_drops_row(monkeypatch, db_session):
    login = _seed_claude(access="old", expires_at=_NOW - timedelta(minutes=1))
    _mock_post(monkeypatch, _resp(400, {"error": "invalid_grant"}))
    result = await ClaudeHarness.rotate_token(login.id, now=_NOW)
    assert result.action == "failed"
    assert result.error == "invalid_grant"
    # A revoked lineage self-disconnects and commits inside the rotation — the
    # deletion never rides (or rolls back with) the tick's later commit.
    assert not HarnessConnection.list_all()
    with pytest.raises(HarnessNotConnectedError):
        ClaudeHarness.get_credentials()


async def test_claude_network_error_keeps_row(monkeypatch, db_session):
    login = _seed_claude(access="old", expires_at=_NOW - timedelta(minutes=1))
    _mock_post(monkeypatch, httpx.ConnectError("boom"))
    result = await ClaudeHarness.rotate_token(login.id, now=_NOW)
    assert result.error == "network"
    assert ClaudeHarness.get_credentials()["claudeAiOauth"]["accessToken"] == "old"


async def test_claude_http_500_keeps_row(monkeypatch, db_session):
    login = _seed_claude(access="old", expires_at=_NOW - timedelta(minutes=1))
    _mock_post(monkeypatch, _resp(500, ""))
    result = await ClaudeHarness.rotate_token(login.id, now=_NOW)
    assert result.error == "http_500"
    assert ClaudeHarness.get_credentials()["claudeAiOauth"]["accessToken"] == "old"


async def test_claude_bad_response_keeps_row(monkeypatch, db_session):
    login = _seed_claude(access="old", expires_at=_NOW - timedelta(minutes=1))
    _mock_post(monkeypatch, _resp(200, "not json"))
    result = await ClaudeHarness.rotate_token(login.id, now=_NOW)
    assert result.error == "bad_response"
    assert ClaudeHarness.get_credentials()["claudeAiOauth"]["accessToken"] == "old"


async def test_codex_invalid_grant_drops_row(monkeypatch, db_session):
    stale = _jwt(int((_NOW + timedelta(hours=1)).timestamp()))
    login = _seed_codex(access=stale, refresh="R0")
    _mock_post(monkeypatch, _resp(400, {"error": "invalid_grant"}))
    result = await CodexHarness.rotate_token(login.id, now=_NOW)
    assert result.error == "invalid_grant"
    assert not HarnessConnection.list_all()
    with pytest.raises(HarnessNotConnectedError):
        CodexHarness.get_credentials()


async def test_rotation_of_a_deleted_row_is_a_no_op(monkeypatch, db_session):
    login = _seed_claude(access="old", expires_at=_NOW - timedelta(minutes=1))
    login_id = login.id
    _mock_post(monkeypatch, _resp(400, {"error": "invalid_grant"}))
    await ClaudeHarness.rotate_token(login_id, now=_NOW)
    # Row is gone; rotating the stale id must short-circuit before any
    # grant POST.
    calls = _mock_post(monkeypatch, _resp(200, {"access_token": "x"}))
    result = await ClaudeHarness.rotate_token(login_id, now=_NOW)
    assert result.action == "failed"
    assert result.error == "no_credentials"
    assert calls == []


async def test_claude_relogin_overwrite_picked_up(monkeypatch, db_session):
    login = _seed_claude(refresh="R_NEW", expires_at=_NOW - timedelta(minutes=1))
    calls = _mock_post(
        monkeypatch, _resp(200, {"access_token": "a", "refresh_token": "b", "expires_in": 100})
    )
    await ClaudeHarness.rotate_token(login.id, now=_NOW)
    assert calls[0]["json"]["refresh_token"] == "R_NEW"


async def test_codex_stale_refreshes_and_preserves(monkeypatch, db_session):
    stale = _jwt(int((_NOW + timedelta(hours=1)).timestamp()))
    fresh = _jwt(int((_NOW + timedelta(days=10)).timestamp()))
    login = _seed_codex(access=stale, refresh="R0", account_id="acc-9")
    calls = _mock_post(
        monkeypatch, _resp(200, {"access_token": fresh, "refresh_token": "R1", "id_token": "id-1"})
    )
    result = await CodexHarness.rotate_token(login.id, now=_NOW)
    assert result.action == "refreshed"
    assert calls[0]["json"]["client_id"] == "app_EMoamEEZ73f0CkXaXp7hrann"
    data = CodexHarness.get_credentials()
    assert data["tokens"]["access_token"] == fresh
    assert data["tokens"]["refresh_token"] == "R1"
    assert data["tokens"]["id_token"] == "id-1"
    assert data["tokens"]["account_id"] == "acc-9"  # preserved
    assert data["auth_mode"] == "chatgpt"  # preserved
    assert "last_refresh" in data


async def test_codex_keeps_refresh_when_omitted(monkeypatch, db_session):
    stale = _jwt(int((_NOW + timedelta(hours=1)).timestamp()))
    fresh = _jwt(int((_NOW + timedelta(days=10)).timestamp()))
    login = _seed_codex(access=stale, refresh="KEEP")
    _mock_post(monkeypatch, _resp(200, {"access_token": fresh}))
    await CodexHarness.rotate_token(login.id, now=_NOW)
    assert CodexHarness.get_credentials()["tokens"]["refresh_token"] == "KEEP"


async def test_codex_no_refresh_token(monkeypatch, db_session):
    stale = _jwt(int((_NOW + timedelta(hours=1)).timestamp()))
    login = _seed_codex(refresh=None, access=stale)
    calls = _mock_post(monkeypatch, _resp(200, {}))
    result = await CodexHarness.rotate_token(login.id, now=_NOW)
    assert result.action == "no_refresh_token"
    assert calls == []


async def test_rotation_touches_only_the_addressed_row(monkeypatch, db_session):
    stale = _seed_claude(
        access="old",
        refresh="R0",
        expires_at=_NOW + timedelta(minutes=30),
        provider_email="a@example.com",
    )
    other = _seed_claude(
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
    assert dict(HarnessConnection.get(stale_id).payload)["claudeAiOauth"]["accessToken"] == "new"
    assert dict(HarnessConnection.get(other_id).payload)["claudeAiOauth"]["accessToken"] == "keep"


async def test_invalid_grant_drops_only_the_addressed_row(monkeypatch, db_session):
    kept = _seed_claude(access="d", expires_at=_NOW - timedelta(minutes=1))
    other = _seed_claude(
        access="o", expires_at=_NOW - timedelta(minutes=1), provider_email="b@example.com"
    )
    kept_id, other_id = kept.id, other.id
    _mock_post(monkeypatch, _resp(400, {"error": "invalid_grant"}))
    await ClaudeHarness.rotate_token(other_id, now=_NOW)
    assert not HarnessConnection.get(other_id)
    assert HarnessConnection.get(kept_id)


async def test_concurrent_rotations_produce_one_grant(monkeypatch, db_session):
    login = _seed_claude(access="old", refresh="R0", expires_at=_NOW + timedelta(minutes=30))
    login_id = login.id
    calls = _mock_post(
        monkeypatch, _resp(200, {"access_token": "new", "refresh_token": "R1", "expires_in": 100})
    )
    first, second = await asyncio.gather(
        ClaudeHarness.rotate_token(login_id, now=_NOW),
        ClaudeHarness.rotate_token(login_id, now=_NOW),
    )
    assert len(calls) == 1  # one provider grant, one persisted lineage
    assert {first.action, second.action} == {"refreshed", "locked"}
    # Sessions are task-scoped: the winner ran (and committed) inside its own
    # gather task, so read past this task's identity map for what persisted.
    assert dict(HarnessConnection.reload(login_id).payload)["claudeAiOauth"]["refreshToken"] == "R1"


async def test_rotation_lock_is_released_after_refresh(monkeypatch, db_session):
    login = _seed_claude(access="old", refresh="R0", expires_at=_NOW + timedelta(minutes=30))
    _mock_post(
        monkeypatch, _resp(200, {"access_token": "new", "refresh_token": "R1", "expires_in": 100})
    )
    await ClaudeHarness.rotate_token(login.id, now=_NOW)
    import druks.redis

    assert await druks.redis.get_client().get(f"druks:harness:refresh:{login.id}") is None


def test_disconnect_removes_only_the_addressed_login(db_session):
    mine = _seed_claude(provider_email="a@example.com")
    other = _seed_claude(provider_email="b@example.com")

    mine.delete()

    assert HarnessConnection.get(other.id)
    # The fallback account (the first) has no claude login left; another
    # account's credential never leaks into execution.
    with pytest.raises(HarnessNotConnectedError):
        ClaudeHarness.get_credentials()


def test_reconnect_restores_execution(db_session):
    mine = _seed_claude(provider_email="a@example.com")
    mine.delete()
    with pytest.raises(HarnessNotConnectedError):
        ClaudeHarness.get_credentials()

    _seed_claude(access="fresh", provider_email="a@example.com")
    assert ClaudeHarness.get_credentials()["claudeAiOauth"]["accessToken"] == "fresh"


def test_connect_scopes_rows_by_harness_and_account(db_session):
    claude_row = _seed_claude(provider_email="a@example.com")
    codex_row = _seed_codex(provider_email="a@example.com")
    other = _seed_claude(provider_email="b@example.com")

    assert len({claude_row.id, codex_row.id, other.id}) == 3
    assert claude_row.account_id == codex_row.account_id  # same person, one account
    assert other.account_id != claude_row.account_id
    assert Account.get_for_email("a@example.com").id == claude_row.account_id
    # The first account adopted the execution fallback.
    assert UserSettings.get().fallback_account_id == claude_row.account_id


def test_reconnect_updates_the_existing_login_in_place(db_session):
    row = _seed_claude(access="old", provider_email="a@example.com")
    # Same email, different case — citext matches it to the existing account,
    # so the reconnect updates that one connection rather than making a second.
    again = _seed_claude(access="new", provider_email="A@Example.com")
    assert again.id == row.id
    assert dict(again.payload)["claudeAiOauth"]["accessToken"] == "new"
    assert again.provider_email == "A@Example.com"  # stored as last given


async def test_claude_fetch_usage_success(monkeypatch, db_session):
    _seed_claude(access="tok", expires_at=_NOW + timedelta(hours=2))
    body = {
        "five_hour": {"utilization": 16.0, "resets_at": "2026-06-04T23:19:59+00:00"},
        "seven_day": {"utilization": 48.0, "resets_at": "2026-06-07T16:00:00+00:00"},
    }
    calls = _mock_get(monkeypatch, _resp(200, body))
    parsed = await ClaudeHarness.fetch_usage(now=_NOW)
    assert parsed.ok is True
    assert parsed.five_hour.percent_left == 84
    assert parsed.week.percent_left == 52
    assert calls[0]["headers"]["Authorization"] == "Bearer tok"
    assert calls[0]["headers"]["anthropic-beta"] == "oauth-2025-04-20"


async def test_claude_fetch_usage_http_error(monkeypatch, db_session):
    _seed_claude(access="tok", expires_at=_NOW + timedelta(hours=2))
    _mock_get(monkeypatch, _resp(403, {"error": "x"}))
    parsed = await ClaudeHarness.fetch_usage(now=_NOW)
    assert parsed.ok is False
    assert parsed.error == "forbidden_scope"


async def test_fetch_usage_no_credentials_skips_http(monkeypatch, db_session):
    calls = _mock_get(monkeypatch, _resp(200, {}))
    parsed = await ClaudeHarness.fetch_usage(now=_NOW)
    assert parsed.ok is False
    assert parsed.error == "no_credentials"
    assert calls == []  # no token => no request


async def test_codex_fetch_usage_success(monkeypatch, db_session):
    _seed_codex(account_id="acc-7")
    body = {
        "plan_type": "prolite",
        "rate_limit": {
            "primary_window": {"used_percent": 39, "reset_at": 1780625132},
            "secondary_window": {"used_percent": 39, "reset_at": 1781211932},
        },
    }
    calls = _mock_get(monkeypatch, _resp(200, body))
    parsed = await CodexHarness.fetch_usage(now=_NOW)
    assert parsed.ok is True
    assert parsed.plan_tier == "prolite"
    assert parsed.five_hour.percent_left == 61
    assert parsed.week.percent_left == 61
    assert calls[0]["headers"]["ChatGPT-Account-Id"] == "acc-7"


def test_render_credentials_file_serializes_stored_payload(db_session):
    login = _seed_claude(access="tok", refresh="R0")
    harness = ClaudeHarness(model=None, fast_mode=False, effort=None, login_id=login.id)
    assert json.loads(harness.render_credentials_file())["claudeAiOauth"]["accessToken"] == "tok"


def test_render_credentials_file_raises_when_not_connected(db_session):
    harness = ClaudeHarness(model=None, fast_mode=False, effort=None)
    with pytest.raises(HarnessNotConnectedError, match="connect it in Settings"):
        harness.render_credentials_file()


def test_claude_builder_puts_db_credentials_on_the_bundle(db_session):
    from pathlib import Path

    from druks.harnesses.claude import _claude_credentials
    from druks.harnesses.datastructures import SandboxSettings

    _seed_claude(access="live", refresh="R0")
    sandbox = SandboxSettings(
        service_url="x",
        service_token="x",
        service_timeout=30.0,
        image="x",
        claude_config_dir=Path("/home/agent/.claude"),
        codex_config_dir=Path("/home/agent/.codex"),
    )
    bundle = _claude_credentials(sandbox, github_token=None)
    assert json.loads(bundle.claude_credentials)["claudeAiOauth"]["accessToken"] == "live"
    assert bundle.codex_credentials is None


def test_no_config_dir_ships_credential_only(db_session):
    # No local config dir for the CLI => nothing of the host's config/plugins
    # reaches the sandbox — but the DB credential still ships: connection state
    # alone decides whether a harness can run.

    from druks.harnesses.claude import _claude_credentials
    from druks.harnesses.datastructures import SandboxSettings

    _seed_claude(access="live")
    sandbox = SandboxSettings(
        service_url="x",
        service_token="x",
        service_timeout=30.0,
        image="x",
        claude_config_dir=None,
        codex_config_dir=None,
    )
    bundle = _claude_credentials(sandbox, github_token="gh")
    assert json.loads(bundle.claude_credentials)["claudeAiOauth"]["accessToken"] == "live"
    assert bundle.extra_config_files == ()
    assert bundle.extra_config_dirs == ()
    assert bundle.github_token == "gh"


def test_claude_builder_raises_when_not_connected(db_session):
    from pathlib import Path

    from druks.harnesses.claude import _claude_credentials
    from druks.harnesses.datastructures import SandboxSettings

    sandbox = SandboxSettings(
        service_url="x",
        service_token="x",
        service_timeout=30.0,
        image="x",
        claude_config_dir=Path("/home/agent/.claude"),
        codex_config_dir=None,
    )
    with pytest.raises(HarnessNotConnectedError, match="claude is not connected"):
        _claude_credentials(sandbox, github_token=None)


def test_select_for_run_uses_the_runs_own_connection(db_session):
    _seed_claude(provider_email="a@example.com")  # a@ adopts the fallback
    other = _seed_claude(provider_email="b@example.com")
    row, reason = HarnessConnection.select_for_run(
        "claude", account_id=other.account_id, unattributed_reason=None
    )
    # The run's own connection — never the fallback account's — and no reason.
    assert (row.id, reason) == (other.id, None)


def test_select_for_run_falls_back_when_the_account_has_no_connection(db_session):
    fallback = _seed_claude(provider_email="a@example.com")
    codex_only = _seed_codex(provider_email="b@example.com")
    row, reason = HarnessConnection.select_for_run(
        "claude", account_id=codex_only.account_id, unattributed_reason=None
    )
    assert (row.id, reason) == (fallback.id, "account_not_connected")


def test_select_for_run_records_the_dispatch_reason(db_session):
    fallback = _seed_claude(provider_email="a@example.com")
    row, reason = HarnessConnection.select_for_run(
        "claude", account_id=None, unattributed_reason="missing_assignee"
    )
    assert (row.id, reason) == (fallback.id, "missing_assignee")
    _, reason = HarnessConnection.select_for_run(
        "claude", account_id=None, unattributed_reason=None
    )
    assert reason == "unattributed"


def test_select_for_run_raises_when_the_fallback_has_no_connection(db_session):
    _seed_codex(provider_email="a@example.com")  # fallback account has codex only
    with pytest.raises(HarnessNotConnectedError, match="connect it in Settings"):
        HarnessConnection.select_for_run("claude", account_id=None, unattributed_reason=None)

import base64
import json
from pathlib import Path

import druks.redis
import httpx
import pytest
from conftest import configure_app_for_test, connect_harness, make_settings
from druks import database
from druks.accounts.models import Account
from druks.accounts.sessions import SESSION_COOKIE
from druks.harnesses import base as hbase
from druks.harnesses.claude import ClaudeHarness
from druks.harnesses.models import HarnessConnection
from druks.user_settings.models import UserSettings
from fastapi.testclient import TestClient
from sqlalchemy import select


@pytest.fixture(autouse=True)
def _clear_redis():
    druks.redis.get_client()._data.clear()
    yield


def _client(tmp_path: Path, **settings_overrides) -> TestClient:
    app = configure_app_for_test(
        settings=make_settings(tmp_path, **settings_overrides), authenticated=False
    )
    return TestClient(app)


def _grant(email: str = "me@example.com") -> dict:
    return {
        "access_token": "AT",
        "refresh_token": "RT",
        "expires_in": 28800,
        "scope": "user:profile",
        "account": {"email_address": email},
    }


def _mock_exchange(monkeypatch, grant: dict):
    async def fake_post(self, url, *, json=None, data=None, **_kwargs):
        return httpx.Response(200, text=_dumps(grant), request=httpx.Request("POST", url))

    monkeypatch.setattr(hbase.httpx.AsyncClient, "post", fake_post)


def _dumps(value: dict) -> str:
    return json.dumps(value)


def _login(client: TestClient, monkeypatch, *, email="me@example.com") -> dict:
    start = client.post("/api/auth/harnesses/claude/login/start")
    assert start.status_code == 200
    _mock_exchange(monkeypatch, _grant(email))
    return client.post(
        "/api/auth/harnesses/claude/login/complete",
        json={"code": "thecode", "loginId": start.json()["loginId"]},
    )


def test_a_sessionless_request_gets_401(tmp_path):
    # One behavioral canary; the boundary enumeration proves the full surface.
    with _client(tmp_path) as client:
        assert client.get("/api/auth/session").status_code == 401


def test_login_flow_mints_session_and_account(tmp_path, monkeypatch, db_session):
    with _client(tmp_path) as client:
        start = client.post("/api/auth/harnesses/claude/login/start")
        assert start.json()["authorizeUrl"].startswith("https://claude.ai/oauth/authorize?")

        response = _login(client, monkeypatch)
        assert response.status_code == 200
        assert response.json()["email"] == "me@example.com"
        cookie = response.headers["set-cookie"]
        assert SESSION_COOKIE in cookie
        assert "HttpOnly" in cookie
        assert "SameSite=lax" in cookie
        assert "Path=/" in cookie

        session = client.get("/api/auth/session")
        assert session.status_code == 200
        assert session.json()["email"] == "me@example.com"


def test_the_session_read_slides_the_cookie(tmp_path, monkeypatch, db_session):
    with _client(tmp_path) as client:
        _login(client, monkeypatch)
        # Every app load hits /session; that re-issue keeps the cookie sliding.
        response = client.get("/api/auth/session")
        assert SESSION_COOKIE in response.headers.get("set-cookie", "")
        assert "Max-Age" in response.headers["set-cookie"]


def test_logout_drops_the_session_and_clears_the_cookie(tmp_path, monkeypatch, db_session):
    with _client(tmp_path) as client:
        _login(client, monkeypatch)
        logout = client.post("/api/auth/logout")
        assert logout.status_code == 204
        assert "Max-Age=0" in logout.headers["set-cookie"]
        assert client.get("/api/auth/session").status_code == 401


def test_login_rotates_any_prior_session_token(tmp_path, monkeypatch, db_session):
    with _client(tmp_path) as client:
        first = _login(client, monkeypatch)
        first_token = client.cookies[SESSION_COOKIE]
        second = _login(client, monkeypatch)
        second_token = client.cookies[SESSION_COOKIE]
        assert first.status_code == second.status_code == 200
        assert first_token != second_token
        # The old token no longer resolves anywhere.
        client.cookies.set(SESSION_COOKIE, first_token)
        assert client.get("/api/auth/session").status_code == 401


def test_redis_eviction_signs_out_but_keeps_credentials(tmp_path, monkeypatch, db_session):
    with _client(tmp_path) as client:
        _login(client, monkeypatch)
        druks.redis.get_client()._data.clear()  # Redis loss
        assert client.get("/api/auth/session").status_code == 401
    # The durable credential is untouched — only the session died.
    assert HarnessConnection.get_for_account("claude", Account.get_for_email("me@example.com").id)


def test_session_keeps_its_account_across_reconnects(tmp_path, monkeypatch, db_session):
    with _client(tmp_path) as client:
        _login(client, monkeypatch, email="me@example.com")
        # The session account wins; the login records the provider identity.
        start = client.post("/api/auth/harnesses/codex/login/start")
        _mock_exchange_codex(monkeypatch, email="corp-seat@corp.com")
        complete = client.post(
            "/api/auth/harnesses/codex/login/complete",
            json={"code": "thecode", "loginId": start.json()["loginId"]},
        )
        assert complete.status_code == 200
        assert complete.json()["email"] == "me@example.com"
    account = Account.get_for_email("me@example.com")
    codex = HarnessConnection.get_for_account("codex", account.id)
    assert codex.provider_email == "corp-seat@corp.com"


def test_bound_reconnect_requires_its_session_at_complete(tmp_path, monkeypatch, db_session):
    with _client(tmp_path) as client:
        _login(client, monkeypatch, email="me@example.com")
        # A reconnect binds its flow to the session account…
        start = client.post("/api/auth/harnesses/codex/login/start")
        # …which dies before paste-back.
        druks.redis.get_client()._data = {
            key: value
            for key, value in druks.redis.get_client()._data.items()
            if not key.startswith("druks:session:")
        }
        _mock_exchange_codex(monkeypatch, email="corp-seat@corp.com")
        response = client.post(
            "/api/auth/harnesses/codex/login/complete",
            json={"code": "thecode", "loginId": start.json()["loginId"]},
        )
        # The flow must never rebind the login by email fallback.
        assert response.status_code == 422
        assert "different session" in response.json()["detail"]
    assert not Account.get_for_email("corp-seat@corp.com")
    assert not any(row.harness == "codex" for row in HarnessConnection.list_all())


def test_two_accounts_may_connect_the_same_provider_login(tmp_path, monkeypatch, db_session):
    # Deliberately unpoliced: the provider email is renameable upstream, so
    # any uniqueness constraint on it would lapse silently.
    connect_harness(
        ClaudeHarness,
        {"claudeAiOauth": {"accessToken": "x"}},
        provider_email="shared@corp.com",
    )
    with _client(tmp_path) as client:
        start = client.post("/api/auth/harnesses/codex/login/start")
        _mock_exchange_codex(monkeypatch, email="me@example.com")
        client.post(
            "/api/auth/harnesses/codex/login/complete",
            json={"code": "thecode", "loginId": start.json()["loginId"]},
        )
        assert _login(client, monkeypatch, email="shared@corp.com").status_code == 200

    claude = [login for login in HarnessConnection.list_all() if login.harness == "claude"]
    assert len(claude) == 2  # one row per account, same provider email
    assert len({login.account_id for login in claude}) == 2


def test_new_identity_cannot_acquire_legacy_logins(tmp_path, monkeypatch, db_session):
    # The migration shape: the dashboard account owns the legacy login.
    legacy = connect_harness(
        ClaudeHarness, {"claudeAiOauth": {"accessToken": "x"}}, provider_email="op@example.com"
    )
    with _client(tmp_path) as client:
        response = _login(client, monkeypatch, email="newcomer@example.com")
        assert response.status_code == 200
    accounts = {account.email: account for account in _all_accounts()}
    assert set(accounts) == {"op@example.com", "newcomer@example.com", "system"}
    # The legacy login stays put; the fallback account stays the operator's.
    assert HarnessConnection.reload(legacy.id).account_id == accounts["op@example.com"].id
    assert UserSettings.get().fallback_account_id == accounts["op@example.com"].id


def test_dashboard_identity_resolves_the_migrated_account(tmp_path, monkeypatch, db_session):
    legacy = connect_harness(
        ClaudeHarness, {"claudeAiOauth": {"accessToken": "x"}}, provider_email="op@example.com"
    )
    legacy_id = legacy.id
    with _client(tmp_path) as client:
        response = _login(client, monkeypatch, email="op@example.com")
        assert response.status_code == 200
    # No second account minted, the legacy login updated in place; reload()
    # reads past this task's identity map.
    assert {account.email for account in _all_accounts()} == {"op@example.com", "system"}
    updated = HarnessConnection.reload(legacy_id)
    assert dict(updated.payload)["claudeAiOauth"]["accessToken"] == "AT"


def _all_accounts() -> list[Account]:
    return list(database.db_session().scalars(select(Account)))


def _mock_exchange_codex(monkeypatch, *, email: str):
    claims = {
        "https://api.openai.com/auth": {"chatgpt_account_id": "acc-1"},
        "https://api.openai.com/profile": {"email": email},
        "exp": 4102444800,
    }
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').rstrip(b"=").decode()
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    grant = {"access_token": f"{header}.{payload}.sig", "refresh_token": "RT", "id_token": "ID"}

    async def fake_post(self, url, *, json=None, data=None, **_kwargs):
        return httpx.Response(200, text=_dumps(grant), request=httpx.Request("POST", url))

    monkeypatch.setattr(hbase.httpx.AsyncClient, "post", fake_post)

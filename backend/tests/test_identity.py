import base64
import json
from pathlib import Path

import druks.redis
import httpx
import pytest
from conftest import configure_app_for_test, connect_harness, make_settings
from druks import database
from druks.accounts.dependencies import resolve_single_operator
from druks.accounts.exceptions import AuthConfigurationError
from druks.accounts.models import Account, PersonalAccessToken
from druks.harnesses import base as hbase
from druks.harnesses.claude import ClaudeHarness
from druks.harnesses.models import HarnessConnection
from druks.user_settings.models import HarnessSettings, UserSettings
from fastapi.testclient import TestClient
from sqlalchemy import select

HEADER = "X-ExeDev-Email"


@pytest.fixture(autouse=True)
def _clear_redis():
    druks.redis.get_client()._data.clear()
    yield


def _client(tmp_path: Path, **settings_overrides) -> TestClient:
    app = configure_app_for_test(
        settings=make_settings(tmp_path, **settings_overrides), authenticated=False
    )
    return TestClient(app)


def _header_client(tmp_path: Path) -> TestClient:
    return _client(tmp_path, auth_mode="header", auth_header=HEADER)


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


def _connect(
    client: TestClient,
    monkeypatch,
    *,
    harness: str = "claude",
    email: str = "me@example.com",
    headers: dict[str, str] | None = None,
):
    start = client.post(f"/api/harnesses/{harness}/connection/start", headers=headers)
    assert start.status_code == 200
    if harness == "claude":
        _mock_exchange(monkeypatch, _grant(email))
    else:
        _mock_exchange_codex(monkeypatch, email=email)
    return client.post(
        f"/api/harnesses/{harness}/connection/complete",
        json={"code": "thecode", "connectionId": start.json()["connectionId"]},
        headers=headers,
    )


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


# --- header mode -----------------------------------------------------------


def test_header_mode_requires_exactly_one_nonblank_assertion(tmp_path, db_session):
    with _header_client(tmp_path) as client:
        assert client.get("/api/auth/me").status_code == 401
        assert client.get("/api/settings").status_code == 401
        assert client.get("/api/auth/me", headers={HEADER: "   "}).status_code == 401
        two = client.get("/api/auth/me", headers=[(HEADER, "a@x.com"), (HEADER, "b@x.com")])
        assert two.status_code == 401
    # Rejection never enrolls anyone.
    assert {account.username for account in _all_accounts()} == {"system"}


def test_an_asserted_email_open_enrolls_once_across_case_variants(tmp_path, db_session):
    with _header_client(tmp_path) as client:
        first = client.get("/api/auth/me", headers={HEADER: "  Op@Example.com "})
        assert first.status_code == 200
        body = first.json()
        assert body["authMode"] == "header"
        assert body["account"]["username"] == "Op@Example.com"
        assert body["onboardingRequired"] is True

        again = client.get("/api/auth/me", headers={HEADER: "op@example.COM"})
        assert again.json()["account"]["id"] == body["account"]["id"]
    assert len(Account.list_non_system()) == 1


def test_get_or_create_losing_the_insert_race_still_converges(db_session, monkeypatch):
    existing = Account.get_or_create("race@example.com")
    # Simulate losing the read-then-insert race: the pre-read misses, the
    # INSERT hits ON CONFLICT DO NOTHING, the canonical lookup converges.
    monkeypatch.setattr(Account, "get_for_username", classmethod(lambda cls, username: None))
    assert Account.get_or_create("Race@example.com").id == existing.id
    assert len(Account.list_non_system()) == 1


def test_a_valid_pat_wins_over_a_conflicting_header(tmp_path, db_session):
    agent = Account.get_or_create("agent@example.com")
    _, token = PersonalAccessToken.create(account_id=agent.id, name="agent")
    with _header_client(tmp_path) as client:
        response = client.get(
            "/api/auth/me",
            headers={"Authorization": f"Bearer {token}", HEADER: "op@example.com"},
        )
        assert response.status_code == 200
        assert response.json()["account"]["username"] == "agent@example.com"
    # The losing assertion never enrolled.
    assert not Account.get_for_username("op@example.com")


@pytest.mark.parametrize("header", ["", "Token abc", "Bearer", "Bearer a b", "Bearer garbage"])
def test_a_bad_authorization_never_falls_through_to_the_assertion(tmp_path, db_session, header):
    with _header_client(tmp_path) as client:
        response = client.get(
            "/api/auth/me", headers={"Authorization": header, HEADER: "op@example.com"}
        )
        assert response.status_code == 401
        assert response.headers["WWW-Authenticate"].startswith('Bearer realm="druks"')
    assert not Account.get_for_username("op@example.com")


def test_onboarding_clears_once_the_account_has_a_connection(tmp_path, db_session):
    connect_harness(ClaudeHarness, {"claudeAiOauth": {"accessToken": "x"}})
    with _header_client(tmp_path) as client:
        body = client.get("/api/auth/me", headers={HEADER: "op@example.com"}).json()
    assert body["onboardingRequired"] is False


# --- none mode -------------------------------------------------------------


def test_none_mode_ignores_a_present_identity_header(tmp_path, db_session):
    connect_harness(ClaudeHarness, {"claudeAiOauth": {"accessToken": "x"}})
    with _client(tmp_path) as client:
        body = client.get("/api/auth/me", headers={HEADER: "intruder@example.com"}).json()
    assert body["authMode"] == "none"
    assert body["account"]["username"] == "op@example.com"
    # Never open-enrolls in none mode.
    assert not Account.get_for_username("intruder@example.com")


def test_none_zero_reads_as_setup(tmp_path, db_session):
    with _client(tmp_path) as client:
        me = client.get("/api/auth/me")
        assert me.status_code == 200
        assert me.json() == {"authMode": "none", "account": None, "onboardingRequired": True}
        assert client.get("/api/settings").status_code == 409


def test_none_one_resolves_the_operator(tmp_path, db_session):
    connect_harness(ClaudeHarness, {"claudeAiOauth": {"accessToken": "x"}})
    with _client(tmp_path) as client:
        body = client.get("/api/auth/me").json()
        assert body["account"]["username"] == "op@example.com"
        assert body["onboardingRequired"] is False
        assert client.get("/api/settings").status_code == 200


def test_none_multi_refuses_requests_and_startup(tmp_path, db_session):
    Account.get_or_create("one@example.com")
    Account.get_or_create("two@example.com")
    with _client(tmp_path) as client:
        assert client.get("/api/settings").status_code == 503
    # The startup validator runs the same check and refuses boot.
    with pytest.raises(AuthConfigurationError):
        resolve_single_operator()


# --- connection flow -------------------------------------------------------


def test_none_zero_setup_flow_creates_the_operator(tmp_path, monkeypatch, db_session):
    with _client(tmp_path) as client:
        response = _connect(client, monkeypatch, email="me@example.com")
        assert response.status_code == 200
        assert response.json()["username"] == "me@example.com"
        assert "set-cookie" not in response.headers
        # The created operator now resolves and is past onboarding.
        body = client.get("/api/auth/me").json()
        assert body["account"]["username"] == "me@example.com"
        assert body["onboardingRequired"] is False
    account = Account.get_for_username("me@example.com")
    assert UserSettings.get().fallback_account_id == account.id
    assert HarnessConnection.get_for_account("claude", account.id)


def test_concurrent_setup_completions_with_one_email_converge(tmp_path, monkeypatch, db_session):
    with _client(tmp_path) as client:
        # Both flows start while zero accounts exist — both unbound.
        first = client.post("/api/harnesses/claude/connection/start")
        second = client.post("/api/harnesses/codex/connection/start")
        _mock_exchange(monkeypatch, _grant("me@example.com"))
        assert (
            client.post(
                "/api/harnesses/claude/connection/complete",
                json={"code": "c1", "connectionId": first.json()["connectionId"]},
            ).status_code
            == 200
        )
        _mock_exchange_codex(monkeypatch, email="me@example.com")
        assert (
            client.post(
                "/api/harnesses/codex/connection/complete",
                json={"code": "c2", "connectionId": second.json()["connectionId"]},
            ).status_code
            == 200
        )
    assert len(Account.list_non_system()) == 1
    assert len(HarnessConnection.list_all()) == 2


def test_a_stale_unbound_completion_attaches_to_the_operator(tmp_path, monkeypatch, db_session):
    with _client(tmp_path) as client:
        # Both flows start while zero accounts exist; the first completion
        # creates the operator, so the second — a different provider email —
        # must attach to that operator instead of minting a rival account and
        # bricking none mode.
        first = client.post("/api/harnesses/claude/connection/start")
        second = client.post("/api/harnesses/codex/connection/start")
        _mock_exchange(monkeypatch, _grant("a@example.com"))
        client.post(
            "/api/harnesses/claude/connection/complete",
            json={"code": "c1", "connectionId": first.json()["connectionId"]},
        )
        _mock_exchange_codex(monkeypatch, email="b@example.com")
        completed = client.post(
            "/api/harnesses/codex/connection/complete",
            json={"code": "c2", "connectionId": second.json()["connectionId"]},
        )
        assert completed.status_code == 200
        assert completed.json()["username"] == "a@example.com"
        assert client.get("/api/settings").status_code == 200
    operator = Account.get_for_username("a@example.com")
    assert len(Account.list_non_system()) == 1
    codex_connection = HarnessConnection.get_for_account("codex", operator.id)
    # The capability keeps its own provider identity; it never rekeys the account.
    assert codex_connection.provider_email == "b@example.com"


def test_a_connect_survives_a_failed_model_refresh(tmp_path, monkeypatch, db_session):
    async def _refresh_boom(self, connection):
        raise RuntimeError("picker flush failed")

    # The credential commits before the refresh runs; a refresh failure past
    # that point must not turn the durable connect into a client-visible error.
    monkeypatch.setattr(HarnessSettings, "refresh_models", _refresh_boom)
    with _client(tmp_path) as client:
        response = _connect(client, monkeypatch, email="me@example.com")
        assert response.status_code == 200
    account = Account.get_for_username("me@example.com")
    assert HarnessConnection.get_for_account("claude", account.id)


def test_a_bound_connect_cannot_complete_under_another_operator(tmp_path, monkeypatch, db_session):
    with _header_client(tmp_path) as client:
        start = client.post(
            "/api/harnesses/claude/connection/start", headers={HEADER: "alice@example.com"}
        )
        _mock_exchange(monkeypatch, _grant("seat@corp.com"))
        response = client.post(
            "/api/harnesses/claude/connection/complete",
            json={"code": "thecode", "connectionId": start.json()["connectionId"]},
            headers={HEADER: "bob@example.com"},
        )
        assert response.status_code == 422
        assert "different operator" in response.json()["detail"]
    assert not any(row.harness == "claude" for row in HarnessConnection.list_all())


def test_first_connection_claims_the_fallback_slot_once(tmp_path, monkeypatch, db_session):
    with _header_client(tmp_path) as client:
        _connect(client, monkeypatch, email="seat@corp.com", headers={HEADER: "first@example.com"})
        first = Account.get_for_username("first@example.com")
        assert UserSettings.get().fallback_account_id == first.id
        _connect(
            client,
            monkeypatch,
            harness="codex",
            email="other-seat@corp.com",
            headers={HEADER: "second@example.com"},
        )
    # The fallback stays with the first operator.
    assert UserSettings.get().fallback_account_id == first.id


def test_reconnect_records_provider_email_but_keeps_the_operator(tmp_path, monkeypatch, db_session):
    with _header_client(tmp_path) as client:
        response = _connect(
            client,
            monkeypatch,
            harness="codex",
            email="corp-seat@corp.com",
            headers={HEADER: "me@example.com"},
        )
        assert response.status_code == 200
        assert response.json()["username"] == "me@example.com"
    account = Account.get_for_username("me@example.com")
    codex = HarnessConnection.get_for_account("codex", account.id)
    assert codex.provider_email == "corp-seat@corp.com"


def test_connection_flow_rejects_a_bearer(tmp_path, db_session):
    agent = Account.get_or_create("agent@example.com")
    _, token = PersonalAccessToken.create(account_id=agent.id, name="agent")
    with _client(tmp_path) as client:
        response = client.post(
            "/api/harnesses/claude/connection/start",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 401


# --- the old browser-session surface is gone -------------------------------


def test_the_session_era_routes_are_gone(tmp_path, db_session):
    with _client(tmp_path) as client:
        gone = [
            client.get("/api/auth/session"),
            client.post("/api/auth/logout"),
            client.post("/api/auth/harnesses/claude/login/start"),
            client.post("/api/auth/harnesses/claude/login/complete"),
        ]
        for response in gone:
            assert response.status_code == 404
            assert "set-cookie" not in response.headers
        # The identity read never mints a cookie either.
        assert "set-cookie" not in client.get("/api/auth/me").headers

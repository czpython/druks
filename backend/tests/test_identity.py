import base64
import json
from pathlib import Path

import httpx
import pytest
from conftest import connect_provider
from druks import database
from druks.accounts.dependencies import resolve_single_operator
from druks.accounts.exceptions import AuthConfigurationError
from druks.accounts.models import Account, PersonalAccessToken
from druks.harnesses import providers as pbase
from druks.harnesses.models import ProviderLogin
from druks.harnesses.providers import AnthropicProvider
from druks.testing import configure_app_for_test, make_settings
from druks.user_settings.models import UserSettings
from fastapi.testclient import TestClient
from sqlalchemy import select

HEADER = "X-ExeDev-Email"


def _client(tmp_path: Path, **settings_overrides) -> TestClient:
    app = configure_app_for_test(
        settings=make_settings(tmp_path, **settings_overrides), authenticated=False
    )
    return TestClient(app)


def _header_client(tmp_path: Path) -> TestClient:
    return _client(tmp_path, identity={"mode": "header", "header": HEADER})


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

    monkeypatch.setattr(pbase.httpx.AsyncClient, "post", fake_post)


def _dumps(value: dict) -> str:
    return json.dumps(value)


def _connect(
    client: TestClient,
    monkeypatch,
    *,
    provider: str = "anthropic",
    email: str = "me@example.com",
    headers: dict[str, str] | None = None,
):
    start = client.post(f"/api/providers/{provider}/connection/start", headers=headers)
    assert start.status_code == 200
    if provider == "anthropic":
        _mock_exchange(monkeypatch, _grant(email))
    else:
        _mock_exchange_codex(monkeypatch, email=email)
    return client.post(
        f"/api/providers/{provider}/connection/complete",
        json={"code": "thecode", "connectionId": start.json()["connectionId"]},
        headers=headers,
    )


async def _all_accounts() -> list[Account]:
    return list(await database.db_session().scalars(select(Account)))


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

    monkeypatch.setattr(pbase.httpx.AsyncClient, "post", fake_post)


# --- header mode -----------------------------------------------------------


async def test_header_mode_requires_exactly_one_nonblank_assertion(tmp_path, druks_db):
    with _header_client(tmp_path) as client:
        assert client.get("/api/auth/me").status_code == 401
        assert client.get("/api/settings").status_code == 401
        assert client.get("/api/auth/me", headers={HEADER: "   "}).status_code == 401
        two = client.get(
            "/api/auth/me", headers=[(HEADER, "a@example.com"), (HEADER, "b@example.com")]
        )
        assert two.status_code == 401
    # Rejection never enrolls anyone.
    assert {account.username for account in await _all_accounts()} == {"system"}


async def test_an_asserted_email_open_enrolls_once_across_case_variants(tmp_path, druks_db):
    with _header_client(tmp_path) as client:
        first = client.get("/api/auth/me", headers={HEADER: "  Op@Example.com "})
        assert first.status_code == 200
        body = first.json()
        assert body["authMode"] == "header"
        assert body["account"]["username"] == "Op@Example.com"
        assert body["onboardingRequired"] is True

        again = client.get("/api/auth/me", headers={HEADER: "op@example.COM"})
        assert again.json()["account"]["id"] == body["account"]["id"]
    assert len(await Account.list_non_system()) == 1


async def test_get_or_create_losing_the_insert_race_still_converges(druks_db, monkeypatch):
    existing = await Account.get_or_create("race@example.com")
    # Simulate losing the read-then-insert race: the pre-read misses, the
    # INSERT hits ON CONFLICT DO NOTHING, the canonical lookup converges.

    async def _miss(cls, username):
        return None

    monkeypatch.setattr(Account, "get_for_username", classmethod(_miss))
    assert (await Account.get_or_create("Race@example.com")).id == existing.id
    assert len(await Account.list_non_system()) == 1


async def test_a_valid_pat_wins_over_a_conflicting_header(tmp_path, druks_db):
    agent = await Account.get_or_create("agent@example.com")
    _, token = await PersonalAccessToken.create(account_id=agent.id, name="agent")
    with _header_client(tmp_path) as client:
        response = client.get(
            "/api/auth/me",
            headers={"Authorization": f"Bearer {token}", HEADER: "op@example.com"},
        )
        assert response.status_code == 200
        assert response.json()["account"]["username"] == "agent@example.com"
    # The losing assertion never enrolled.
    assert not await Account.get_for_username("op@example.com")


async def test_onboarding_clears_once_the_account_has_a_connection(tmp_path, druks_db):
    await connect_provider(AnthropicProvider, {"claudeAiOauth": {"accessToken": "x"}})
    with _header_client(tmp_path) as client:
        body = client.get("/api/auth/me", headers={HEADER: "op@example.com"}).json()
    assert body["onboardingRequired"] is False


# --- none mode -------------------------------------------------------------


async def test_none_mode_ignores_a_present_identity_header(tmp_path, druks_db):
    await connect_provider(AnthropicProvider, {"claudeAiOauth": {"accessToken": "x"}})
    with _client(tmp_path) as client:
        body = client.get("/api/auth/me", headers={HEADER: "intruder@example.com"}).json()
    assert body["authMode"] == "none"
    assert body["account"]["username"] == "op@example.com"
    # Never open-enrolls in none mode.
    assert not await Account.get_for_username("intruder@example.com")


def test_none_zero_reads_as_setup(tmp_path, druks_db):
    with _client(tmp_path) as client:
        me = client.get("/api/auth/me")
        assert me.status_code == 200
        assert me.json() == {"authMode": "none", "account": None, "onboardingRequired": True}
        assert client.get("/api/settings").status_code == 409


async def test_none_one_resolves_the_operator(tmp_path, druks_db):
    await connect_provider(AnthropicProvider, {"claudeAiOauth": {"accessToken": "x"}})
    with _client(tmp_path) as client:
        body = client.get("/api/auth/me").json()
        assert body["account"]["username"] == "op@example.com"
        assert body["onboardingRequired"] is False
        assert client.get("/api/settings").status_code == 200


async def test_none_multi_refuses_requests_and_startup(tmp_path, druks_db):
    await Account.get_or_create("one@example.com")
    await Account.get_or_create("two@example.com")
    with _client(tmp_path) as client:
        assert client.get("/api/settings").status_code == 503
    # The startup validator runs the same check and refuses boot.
    with pytest.raises(AuthConfigurationError):
        await resolve_single_operator()


# --- connection flow -------------------------------------------------------


async def test_none_zero_setup_flow_creates_the_operator(tmp_path, monkeypatch, druks_db):
    with _client(tmp_path) as client:
        response = _connect(client, monkeypatch, email="me@example.com")
        assert response.status_code == 200
        assert response.json()["username"] == "me@example.com"
        assert "set-cookie" not in response.headers
        # The created operator now resolves and is past onboarding.
        body = client.get("/api/auth/me").json()
        assert body["account"]["username"] == "me@example.com"
        assert body["onboardingRequired"] is False
    account = await Account.get_for_username("me@example.com")
    assert (await UserSettings.get()).fallback_account_id == account.id
    assert await ProviderLogin.get_for_account("anthropic", account.id)


async def test_api_key_connect_stores_the_operator_identity(tmp_path, druks_db):
    with _header_client(tmp_path) as client:
        response = client.post(
            "/api/providers/anthropic/connection",
            json={"key": "  api-key-value  "},
            headers={HEADER: "operator@example.com"},
        )

    assert response.status_code == 200
    assert response.json()["kind"] == "api_key"
    assert "api-key-value" not in response.text
    account = await Account.get_for_username("operator@example.com")
    connection = await ProviderLogin.get_for_account("anthropic", account.id)
    assert connection.kind == "api_key"
    assert connection.payload == {"api_key": "api-key-value"}
    assert connection.provider_email == account.username
    assert not connection.expires_at


async def test_api_key_connect_requires_a_declared_kind(tmp_path, druks_db):
    with _header_client(tmp_path) as client:
        response = client.post(
            "/api/providers/openai-codex/connection",
            json={"key": "api-key-value"},
            headers={HEADER: "operator@example.com"},
        )

    assert response.status_code == 422


async def test_api_key_connect_rejects_an_empty_key(tmp_path, druks_db):
    with _header_client(tmp_path) as client:
        response = client.post(
            "/api/providers/anthropic/connection",
            json={"key": "   "},
            headers={HEADER: "operator@example.com"},
        )

    assert response.status_code == 422


async def test_api_key_connect_refuses_setup_scope(tmp_path, druks_db):
    with _client(tmp_path) as client:
        response = client.post(
            "/api/providers/anthropic/connection",
            json={"key": "api-key-value"},
        )

    assert response.status_code == 409
    assert not await Account.list_non_system()
    assert not await ProviderLogin.list_all()


async def test_concurrent_setup_completions_with_one_email_converge(
    tmp_path, monkeypatch, druks_db
):
    with _client(tmp_path) as client:
        # Both flows start while zero accounts exist — both unbound.
        first = client.post("/api/providers/anthropic/connection/start")
        second = client.post("/api/providers/openai-codex/connection/start")
        _mock_exchange(monkeypatch, _grant("me@example.com"))
        assert (
            client.post(
                "/api/providers/anthropic/connection/complete",
                json={"code": "c1", "connectionId": first.json()["connectionId"]},
            ).status_code
            == 200
        )
        _mock_exchange_codex(monkeypatch, email="me@example.com")
        assert (
            client.post(
                "/api/providers/openai-codex/connection/complete",
                json={"code": "c2", "connectionId": second.json()["connectionId"]},
            ).status_code
            == 200
        )
    assert len(await Account.list_non_system()) == 1
    assert len(await ProviderLogin.list_all()) == 2


async def test_a_stale_unbound_completion_attaches_to_the_operator(tmp_path, monkeypatch, druks_db):
    with _client(tmp_path) as client:
        # Both flows start while zero accounts exist; the first completion
        # creates the operator, so the second — a different provider email —
        # must attach to that operator instead of minting a rival account and
        # bricking none mode.
        first = client.post("/api/providers/anthropic/connection/start")
        second = client.post("/api/providers/openai-codex/connection/start")
        _mock_exchange(monkeypatch, _grant("a@example.com"))
        client.post(
            "/api/providers/anthropic/connection/complete",
            json={"code": "c1", "connectionId": first.json()["connectionId"]},
        )
        _mock_exchange_codex(monkeypatch, email="b@example.com")
        completed = client.post(
            "/api/providers/openai-codex/connection/complete",
            json={"code": "c2", "connectionId": second.json()["connectionId"]},
        )
        assert completed.status_code == 200
        assert completed.json()["username"] == "a@example.com"
        assert client.get("/api/settings").status_code == 200
    operator = await Account.get_for_username("a@example.com")
    assert len(await Account.list_non_system()) == 1
    codex_connection = await ProviderLogin.get_for_account("openai-codex", operator.id)
    # The capability keeps its own provider identity; it never rekeys the account.
    assert codex_connection.provider_email == "b@example.com"


async def test_a_connect_survives_a_failed_catalog_refresh(tmp_path, monkeypatch, druks_db):
    async def _refresh_boom(cls, login):
        raise RuntimeError("picker flush failed")

    # The login commits before the refresh runs; a refresh failure past
    # that point must not turn the durable connect into a client-visible error.
    monkeypatch.setattr(AnthropicProvider, "refresh_catalog", classmethod(_refresh_boom))
    with _client(tmp_path) as client:
        response = _connect(client, monkeypatch, email="me@example.com")
        assert response.status_code == 200
    account = await Account.get_for_username("me@example.com")
    assert await ProviderLogin.get_for_account("anthropic", account.id)


async def test_a_bound_connect_cannot_complete_under_another_operator(
    tmp_path, monkeypatch, druks_db
):
    with _header_client(tmp_path) as client:
        start = client.post(
            "/api/providers/anthropic/connection/start", headers={HEADER: "alice@example.com"}
        )
        _mock_exchange(monkeypatch, _grant("seat@corp.com"))
        response = client.post(
            "/api/providers/anthropic/connection/complete",
            json={"code": "thecode", "connectionId": start.json()["connectionId"]},
            headers={HEADER: "bob@example.com"},
        )
        assert response.status_code == 422
        assert "different operator" in response.json()["detail"]
    assert not any(row.provider == "anthropic" for row in await ProviderLogin.list_all())


async def test_first_connection_claims_the_fallback_slot_once(tmp_path, monkeypatch, druks_db):
    with _header_client(tmp_path) as client:
        _connect(client, monkeypatch, email="seat@corp.com", headers={HEADER: "first@example.com"})
        first = await Account.get_for_username("first@example.com")
        assert (await UserSettings.get()).fallback_account_id == first.id
        _connect(
            client,
            monkeypatch,
            provider="openai-codex",
            email="other-seat@corp.com",
            headers={HEADER: "second@example.com"},
        )
    # The fallback stays with the first operator.
    assert (await UserSettings.get()).fallback_account_id == first.id


async def test_reconnect_records_provider_email_but_keeps_the_operator(
    tmp_path, monkeypatch, druks_db
):
    with _header_client(tmp_path) as client:
        response = _connect(
            client,
            monkeypatch,
            provider="openai-codex",
            email="corp-seat@corp.com",
            headers={HEADER: "me@example.com"},
        )
        assert response.status_code == 200
        assert response.json()["username"] == "me@example.com"
    account = await Account.get_for_username("me@example.com")
    codex = await ProviderLogin.get_for_account("openai-codex", account.id)
    assert codex.provider_email == "corp-seat@corp.com"


async def test_connection_flow_rejects_a_bearer(tmp_path, druks_db):
    agent = await Account.get_or_create("agent@example.com")
    _, token = await PersonalAccessToken.create(account_id=agent.id, name="agent")
    with _client(tmp_path) as client:
        response = client.post(
            "/api/providers/anthropic/connection/start",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 401


# --- the old browser-session surface is gone -------------------------------


def test_the_session_era_routes_are_gone(tmp_path, druks_db):
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

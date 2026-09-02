from datetime import UTC, datetime, timedelta
from pathlib import Path

from conftest import connect_provider
from druks.accounts.models import Account
from druks.harnesses.models import ProviderCatalog, ProviderLogin
from druks.harnesses.providers import AnthropicProvider
from druks.testing import configure_app_for_test, make_settings
from fastapi.testclient import TestClient


def _build_client(tmp_path: Path) -> TestClient:
    return TestClient(configure_app_for_test(settings=make_settings(tmp_path)))


def _providers(client: TestClient) -> dict[str, dict]:
    return {p["id"]: p for p in client.get("/api/providers").json()}


def test_list_carries_login_kinds_and_no_connection(tmp_path: Path):
    with _build_client(tmp_path) as client:
        providers = _providers(client)
    assert providers["anthropic"]["loginKinds"] == ["api_key", "oauth"]
    assert providers["openai-codex"]["loginKinds"] == ["oauth"]
    assert providers["openai"]["label"] == "OpenAI"


async def test_list_shows_only_the_requesting_accounts_login(tmp_path: Path, druks_db):
    # The list resolves the edge identity itself; another account's login
    # never shows on this card.
    await connect_provider(
        AnthropicProvider,
        {"claudeAiOauth": {"accessToken": "x"}},
        provider_email="someone-else@example.com",
    )
    settings = make_settings(tmp_path, identity={"mode": "header", "header": "X-Edge-Email"})
    with TestClient(configure_app_for_test(settings=settings)) as client:
        response = client.get("/api/providers/logins", headers={"X-Edge-Email": "op@example.com"})
    assert response.json() == []


async def test_logins_report_the_provider_identity(tmp_path: Path, druks_db):
    # The provider identity is display, never authority.
    await ProviderLogin.connect(
        provider="anthropic",
        account=await Account.get_or_create("op@example.com"),
        payload={"claudeAiOauth": {"accessToken": "x"}},
        expires_at=None,
        provider_email="seat@corp.com",
        kind="oauth",
    )
    with _build_client(tmp_path) as client:
        logins = client.get("/api/providers/logins").json()
    assert logins == [
        {
            "provider": "anthropic",
            "kind": "oauth",
            "providerEmail": "seat@corp.com",
            "expiresAt": None,
            "connected": True,
        }
    ]


async def test_logins_read_an_expired_token_as_not_connected(tmp_path: Path, druks_db):
    await ProviderLogin.connect(
        provider="anthropic",
        account=await Account.get_or_create("op@example.com"),
        payload={"claudeAiOauth": {"accessToken": "x"}},
        expires_at=datetime.now(UTC) - timedelta(hours=1),
        provider_email="seat@corp.com",
        kind="oauth",
    )
    with _build_client(tmp_path) as client:
        [login] = client.get("/api/providers/logins").json()
    assert login["connected"] is False


async def test_disconnect_removes_only_the_requesting_accounts_login(tmp_path: Path, druks_db):
    mine = await connect_provider(AnthropicProvider, {"claudeAiOauth": {"accessToken": "x"}})
    other = await connect_provider(
        AnthropicProvider,
        {"claudeAiOauth": {"accessToken": "y"}},
        provider_email="someone-else@example.com",
    )
    mine_id, other_id = mine.id, other.id
    with _build_client(tmp_path) as client:
        response = client.delete("/api/providers/anthropic/connection")
    assert response.status_code == 204
    # The request deleted in its own task-scoped session; read past this
    # task's identity map for what actually persisted.
    assert not await ProviderLogin.reload(mine_id)
    assert await ProviderLogin.reload(other_id)


def test_disconnect_without_a_login_is_a_no_op(tmp_path: Path):
    with _build_client(tmp_path) as client:
        response = client.delete("/api/providers/anthropic/connection")
    assert response.status_code == 204


def test_unknown_provider_is_404(tmp_path: Path):
    with _build_client(tmp_path) as client:
        assert client.delete("/api/providers/nope/connection").status_code == 404


async def test_catalogs_list_what_each_provider_offers(tmp_path: Path, druks_db):
    await ProviderCatalog.store(
        "anthropic", [{"id": "anthropic/claude-fable-5", "label": "Claude Fable 5", "efforts": []}]
    )
    await druks_db.commit()
    with _build_client(tmp_path) as client:
        [catalog] = client.get("/api/providers/catalogs").json()
    assert catalog["provider"] == "anthropic"
    assert catalog["models"] == [{"id": "anthropic/claude-fable-5", "label": "Claude Fable 5"}]
    assert catalog["fetchedAt"]

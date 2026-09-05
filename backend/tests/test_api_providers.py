from datetime import UTC, datetime, timedelta
from pathlib import Path

from conftest import connect_provider
from druks.accounts.models import Account
from druks.harnesses import directory
from druks.harnesses.models import ProviderCatalog, ProviderKey, ProviderSubscription
from druks.harnesses.providers import AnthropicProvider
from druks.testing import configure_app_for_test, make_settings
from fastapi.testclient import TestClient


def _build_client(tmp_path: Path) -> TestClient:
    return TestClient(configure_app_for_test(settings=make_settings(tmp_path)))


def _providers(client: TestClient) -> dict[str, dict]:
    return {p["id"]: p for p in client.get("/api/providers").json()}


def test_list_carries_billing_options_and_no_connection(tmp_path: Path):
    # One card per vendor; each takes a subscription and an API key.
    with _build_client(tmp_path) as client:
        providers = _providers(client)
    assert list(providers) == ["anthropic", "openai"]
    assert providers["anthropic"]["billingOptions"] == ["api_key", "subscription"]
    assert providers["openai"]["billingOptions"] == ["api_key", "subscription"]
    assert providers["openai"]["label"] == "OpenAI"


async def test_list_shows_only_the_requesting_accounts_login(tmp_path: Path, druks_db):
    # The list resolves the edge identity itself; another account's subscription
    # never shows on this card.
    await connect_provider(
        AnthropicProvider,
        {"claudeAiOauth": {"accessToken": "x"}},
        provider_email="someone-else@example.com",
    )
    settings = make_settings(tmp_path, identity={"mode": "header", "header": "X-Edge-Email"})
    with TestClient(configure_app_for_test(settings=settings)) as client:
        response = client.get(
            "/api/providers/subscriptions", headers={"X-Edge-Email": "op@example.com"}
        )
    assert response.json() == []


async def test_keys_list_the_installations_keys_for_every_account(tmp_path: Path, druks_db):
    ops = await Account.get_or_create("ops@example.com")
    stored = await ProviderKey.create(provider="openai", key="sk-openai-4f2a", account=ops)
    with _build_client(tmp_path) as client:
        [key] = client.get("/api/providers/keys").json()
    assert key == {
        "provider": "openai",
        "keyTail": "4f2a",
        "updatedBy": {"id": ops.id, "username": "ops@example.com"},
        "updatedAt": stored.updated_at.isoformat().replace("+00:00", "Z"),
    }


async def test_logins_report_the_provider_identity(tmp_path: Path, druks_db):
    # The provider identity is display, never authority.
    await ProviderSubscription.connect(
        provider="anthropic",
        account=await Account.get_or_create("op@example.com"),
        payload={"claudeAiOauth": {"accessToken": "x"}},
        expires_at=None,
        provider_email="seat@corp.com",
    )
    with _build_client(tmp_path) as client:
        [subscription] = client.get("/api/providers/subscriptions").json()
    assert subscription.pop("updatedAt")
    assert subscription == {
        "provider": "anthropic",
        "providerEmail": "seat@corp.com",
        "expiresAt": None,
        "connected": True,
    }


async def test_logins_read_an_expired_token_as_not_connected(tmp_path: Path, druks_db):
    await ProviderSubscription.connect(
        provider="anthropic",
        account=await Account.get_or_create("op@example.com"),
        payload={"claudeAiOauth": {"accessToken": "x"}},
        expires_at=datetime.now(UTC) - timedelta(hours=1),
        provider_email="seat@corp.com",
    )
    with _build_client(tmp_path) as client:
        [subscription] = client.get("/api/providers/subscriptions").json()
    assert subscription["connected"] is False


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
    assert not await ProviderSubscription.reload(mine_id)
    assert await ProviderSubscription.reload(other_id)


async def test_removing_the_key_leaves_every_subscription(tmp_path: Path, druks_db):
    mine = await connect_provider(AnthropicProvider, {"claudeAiOauth": {"accessToken": "x"}})
    await ProviderKey.create(
        provider="anthropic",
        key="sk-shared",
        account=await Account.get_or_create("ops@example.com"),
    )
    mine_id = mine.id
    with _build_client(tmp_path) as client:
        response = client.delete("/api/providers/anthropic/key")
    assert response.status_code == 204
    assert await ProviderKey.list_all() == []
    assert await ProviderSubscription.reload(mine_id)


_GROQ = {
    "provider": "groq",
    "label": "Groq",
    "models": [{"id": "groq/llama-4", "label": "Llama 4"}],
}


def _stub_directory(monkeypatch, providers: list[dict]) -> None:
    async def list_providers():
        return providers

    monkeypatch.setattr(directory, "list_providers", list_providers)


async def test_a_key_for_a_directory_provider_adds_it(tmp_path: Path, druks_db, monkeypatch):
    _stub_directory(monkeypatch, [_GROQ])
    with _build_client(tmp_path) as client:
        response = client.post("/api/providers/groq/key", json={"key": "gsk-secret"})
        assert response.status_code == 200
        assert response.json()["provider"] == "groq"
        [catalog] = client.get("/api/providers/catalogs").json()
        assert (catalog["provider"], catalog["label"]) == ("groq", "Groq")
        assert client.post("/api/providers/nobody/key", json={"key": "x"}).status_code == 404

        # Removing the key removes the provider with it.
        assert client.delete("/api/providers/groq/key").status_code == 204
        assert client.get("/api/providers/catalogs").json() == []
    assert await ProviderKey.list_all() == []


def test_directory_lists_only_providers_one_can_add(tmp_path: Path, monkeypatch):
    _stub_directory(monkeypatch, [_GROQ, {"provider": "openai", "label": "OpenAI", "models": []}])
    with _build_client(tmp_path) as client:
        assert client.get("/api/providers/directory").json() == [_GROQ]


def test_disconnect_without_a_login_is_a_no_op(tmp_path: Path):
    with _build_client(tmp_path) as client:
        response = client.delete("/api/providers/anthropic/connection")
    assert response.status_code == 204


def test_unknown_provider_is_404(tmp_path: Path):
    with _build_client(tmp_path) as client:
        assert client.delete("/api/providers/nope/connection").status_code == 404
        assert client.delete("/api/providers/nope/key").status_code == 404


async def test_catalogs_list_what_each_provider_offers(tmp_path: Path, druks_db):
    await ProviderCatalog.create(
        "anthropic",
        [{"id": "anthropic/claude-fable-5", "label": "Claude Fable 5", "efforts": []}],
        label="Anthropic",
    )
    await druks_db.commit()
    with _build_client(tmp_path) as client:
        [catalog] = client.get("/api/providers/catalogs").json()
    assert catalog["provider"] == "anthropic"
    assert catalog["label"] == "Anthropic"
    assert catalog["models"] == [{"id": "anthropic/claude-fable-5", "label": "Claude Fable 5"}]
    assert catalog["fetchedAt"]

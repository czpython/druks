from datetime import UTC, datetime, timedelta
from pathlib import Path

from conftest import connect_provider
from druks.accounts.models import Account
from druks.harnesses import directory
from druks.harnesses.models import ProviderCatalog
from druks.harnesses.providers import AnthropicProvider
from druks.secrets.datastructures import Audience
from druks.secrets.enums import SecretKind
from druks.secrets.models import VaultSecret
from druks.testing import configure_app_for_test, make_settings
from fastapi.testclient import TestClient


def _build_client(tmp_path: Path) -> TestClient:
    return TestClient(configure_app_for_test(settings=make_settings(tmp_path)))


def test_list_carries_billing_options_and_no_connection(tmp_path: Path):
    with _build_client(tmp_path) as client:
        providers = {provider["id"]: provider for provider in client.get("/api/providers").json()}
    assert list(providers) == ["anthropic", "openai"]
    assert providers["anthropic"]["billingOptions"] == ["api_key", "subscription"]
    assert providers["anthropic"]["signIn"] == "login"
    assert providers["openai"]["billingOptions"] == ["api_key", "subscription"]
    assert providers["openai"]["signIn"] == "login"
    assert providers["openai"]["label"] == "OpenAI"


async def test_list_shows_only_the_requesting_accounts_login(tmp_path: Path, druks_db):
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
    ops = await Account.get_or_create(druks_db, "ops@example.com")
    stored = await VaultSecret.paste(
        druks_db, Audience.provider("openai"), "sk-openai-4f2a", pasted_by=ops
    )
    with _build_client(tmp_path) as client:
        [key] = client.get("/api/providers/keys").json()
    assert key == {
        "provider": "openai",
        "keyTail": "4f2a",
        "updatedBy": {"id": ops.id, "username": "ops@example.com", "isDefault": True},
        "updatedAt": stored.updated_at.isoformat().replace("+00:00", "Z"),
    }


async def test_logins_report_the_provider_identity(tmp_path: Path, druks_db):
    await VaultSecret.store(
        druks_db,
        SecretKind.SUBSCRIPTION,
        Audience.provider("anthropic"),
        account_id=(await Account.get_or_create(druks_db, "op@example.com")).id,
        secrets={"claudeAiOauth": {"accessToken": "x"}},
        identity={"email": "seat@corp.com"},
        expires_at=None,
    )
    with _build_client(tmp_path) as client:
        [subscription] = client.get("/api/providers/subscriptions").json()
    assert subscription.pop("updatedAt")
    assert subscription == {
        "provider": "anthropic",
        "providerEmail": "seat@corp.com",
        "expiresAt": None,
        "lastRefreshedAt": None,
        "connected": True,
        "revokedAt": None,
        "revokedReason": "",
    }


async def test_logins_read_an_expired_token_as_not_connected(tmp_path: Path, druks_db):
    await VaultSecret.store(
        druks_db,
        SecretKind.SUBSCRIPTION,
        Audience.provider("anthropic"),
        account_id=(await Account.get_or_create(druks_db, "op@example.com")).id,
        secrets={"claudeAiOauth": {"accessToken": "x"}},
        identity={"email": "seat@corp.com"},
        expires_at=datetime.now(UTC) - timedelta(hours=1),
    )
    with _build_client(tmp_path) as client:
        [subscription] = client.get("/api/providers/subscriptions").json()
    assert subscription["connected"] is False


async def test_revoked_subscription_keeps_its_facts_for_its_owner(tmp_path: Path, druks_db):
    mine = await connect_provider(AnthropicProvider, {"claudeAiOauth": {"accessToken": "x"}})
    await mine.update_secrets(dict(mine.secrets), expires_at=None)
    await mine.revoke("invalid_grant")
    other = await connect_provider(
        AnthropicProvider,
        {"claudeAiOauth": {"accessToken": "y"}},
        provider_email="someone-else@example.com",
    )
    await other.revoke("invalid_grant")
    assert not await VaultSecret.list_subscriptions(druks_db)

    with _build_client(tmp_path) as client:
        [subscription] = client.get("/api/providers/subscriptions").json()

    assert subscription == {
        "provider": "anthropic",
        "providerEmail": mine.identity["email"],
        "expiresAt": None,
        "updatedAt": mine.updated_at.isoformat().replace("+00:00", "Z"),
        "lastRefreshedAt": mine.last_refreshed_at.isoformat().replace("+00:00", "Z"),
        "connected": False,
        "revokedAt": mine.revoked_at.isoformat().replace("+00:00", "Z"),
        "revokedReason": "invalid_grant",
    }


async def test_disconnect_removes_only_the_requesting_accounts_login(tmp_path: Path, druks_db):
    mine = await connect_provider(AnthropicProvider, {"claudeAiOauth": {"accessToken": "x"}})
    other = await connect_provider(
        AnthropicProvider,
        {"claudeAiOauth": {"accessToken": "y"}},
        provider_email="someone-else@example.com",
    )
    mine_id, other_id = mine.id, other.id
    with _build_client(tmp_path) as client:
        assert client.delete("/api/providers/anthropic/connection").status_code == 204
        assert client.get("/api/providers/subscriptions").json() == []
    # The request revoked in its own session. reload reads past this identity map.
    assert not await VaultSecret.reload(druks_db, mine_id)
    assert await VaultSecret.reload(druks_db, other_id)


async def test_removing_the_key_leaves_every_subscription(tmp_path: Path, druks_db):
    mine = await connect_provider(AnthropicProvider, {"claudeAiOauth": {"accessToken": "x"}})
    await VaultSecret.paste(
        druks_db,
        Audience.provider("anthropic"),
        "sk-shared",
        pasted_by=await Account.get_or_create(druks_db, "ops@example.com"),
    )
    mine_id = mine.id
    with _build_client(tmp_path) as client:
        response = client.delete("/api/providers/anthropic/key")
    assert response.status_code == 204
    assert await VaultSecret.list_keys(druks_db) == []
    assert await VaultSecret.reload(druks_db, mine_id)


_GROQ = {
    "provider": "groq",
    "label": "Groq",
    "documentation_url": "https://console.groq.com/docs",
    "api_url": "https://api.groq.com/openai/v1",
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

        assert client.delete("/api/providers/groq/key").status_code == 204
        assert client.get("/api/providers/catalogs").json() == []
    assert await VaultSecret.list_keys(druks_db) == []


def test_directory_lists_only_providers_one_can_add(tmp_path: Path, monkeypatch):
    _stub_directory(monkeypatch, [_GROQ, {"provider": "openai", "label": "OpenAI", "models": []}])
    with _build_client(tmp_path) as client:
        assert client.get("/api/providers/directory").json() == [
            {
                "provider": "groq",
                "label": "Groq",
                "models": _GROQ["models"],
                "documentationUrl": "https://console.groq.com/docs",
                "apiUrl": "https://api.groq.com/openai/v1",
            }
        ]


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
        druks_db,
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


def _sign_in_with(monkeypatch, tmp_path: Path, method: str) -> None:
    settings = make_settings(tmp_path, providers={"anthropic": {"sign_in": method}})
    monkeypatch.setattr("druks.harnesses.providers.load_settings", lambda: settings)


async def test_a_pasted_setup_token_is_the_requesting_accounts_subscription(
    tmp_path: Path, druks_db, monkeypatch
):
    _sign_in_with(monkeypatch, tmp_path, "setup_token")
    with _build_client(tmp_path) as client:
        response = client.post(
            "/api/providers/anthropic/sign-in/token", json={"token": " sk-ant-oat01-pasted \n"}
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["providerEmail"], body["connected"]) == (None, True)
    expires_at = datetime.fromisoformat(body["expiresAt"])
    assert timedelta(days=364) < expires_at - datetime.now(UTC) <= timedelta(days=365)

    account = await Account.get_or_create(druks_db, "op@example.com")
    row = await VaultSecret.lookup(
        druks_db, SecretKind.SUBSCRIPTION, Audience.provider("anthropic"), account.id
    )
    assert row.secrets["claudeAiOauth"]["accessToken"] == "sk-ant-oat01-pasted"
    assert row.secrets["claudeAiOauth"]["scopes"] == ["user:inference"]
    assert "refreshToken" not in row.secrets["claudeAiOauth"]
    assert row.identity == {"email": None}


def test_a_token_of_another_shape_is_refused(tmp_path: Path, monkeypatch):
    _sign_in_with(monkeypatch, tmp_path, "setup_token")
    with _build_client(tmp_path) as client:
        response = client.post(
            "/api/providers/anthropic/sign-in/token", json={"token": "sk-ant-api03-key"}
        )
        openai = client.post(
            "/api/providers/openai/sign-in/token", json={"token": "sk-ant-oat01-x"}
        )
    assert response.status_code == 422
    assert response.json()["detail"].startswith("That is not a setup token.")
    assert openai.status_code == 422
    assert openai.json()["detail"] == "OpenAI does not sign in with a pasted token."


def test_the_installation_offers_one_sign_in_method(tmp_path: Path, monkeypatch):
    _sign_in_with(monkeypatch, tmp_path, "setup_token")
    with _build_client(tmp_path) as client:
        [anthropic, _] = client.get("/api/providers").json()
        start = client.post("/api/providers/anthropic/connection/start")
        complete = client.post(
            "/api/providers/anthropic/connection/complete",
            json={"code": "code", "connectionId": "flow"},
        )
    assert anthropic["signIn"] == "token"
    assert (start.status_code, complete.status_code) == (422, 422)
    assert start.json()["detail"].startswith(
        "This installation signs in with setup_token, not oauth."
    )

    _sign_in_with(monkeypatch, tmp_path, "oauth")
    with _build_client(tmp_path) as client:
        paste = client.post(
            "/api/providers/anthropic/sign-in/token", json={"token": "sk-ant-oat01-x"}
        )
    assert paste.status_code == 422
    assert paste.json()["detail"].startswith(
        "This installation signs in with oauth, not setup_token."
    )

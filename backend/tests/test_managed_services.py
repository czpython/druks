import json
import traceback

import httpx
import pytest
from conftest import bind_ambient_session, connect_service
from druks.accounts.models import Account
from druks.apps.registry import webhooks
from druks.chat.channels.whatsapp.services import Waha
from druks.secrets.datastructures import Audience
from druks.secrets.models import VaultSecret
from druks.services import Service
from druks.services.exceptions import ServiceConnectError, ServiceManagedError
from druks.settings import Settings
from druks.testing import asgi_client, configure_app_for_test
from druks.webhooks import Webhook
from pydantic import BaseModel, SecretStr, field_validator
from sqlalchemy import select

INSTANCE_ID = "6f2c1c1e-9b8e-4b65-9d3c-7a2b1f4e8d10"
MANAGER = f"""[manager]
name = "Druks Cloud"
jwks_url = "https://portal.test/.well-known/jwks.json"
issuer = "https://portal.test"
audience = "{INSTANCE_ID}"
services = ["github"]
"""


@pytest.fixture
def declared_webhooks():
    saved = dict(webhooks._items)
    yield
    webhooks._items.clear()
    webhooks._items.update(saved)


@pytest.fixture
def acme(declared_services):
    class Acme(Service):
        class Settings(BaseModel):
            url: str
            key: SecretStr

            @field_validator("key")
            @classmethod
            def valid_key(cls, value):
                if value.get_secret_value() == "invalid-secret":
                    raise ValueError(f"Rejected {value.get_secret_value()}")
                return value

    return Acme


@pytest.fixture
def configuration(tmp_path, monkeypatch):
    path = tmp_path / "druks.toml"
    path.write_text("")
    monkeypatch.setenv("DRUKS_CONFIG", str(path))
    return path


@pytest.fixture
def secrets(tmp_path, monkeypatch):
    path = tmp_path / "secrets"
    path.mkdir()
    monkeypatch.setenv("DRUKS_SECRETS_DIR", str(path))
    return path


def test_service_entry_takes_its_secrets_from_files(configuration, secrets):
    configuration.write_text('[services.acme]\nurl = "https://acme.test"\n')
    (secrets / "services.acme.key").write_text("acme-secret")
    (secrets / "services.other.key").write_text("unused-secret")

    settings = Settings()

    assert settings.services == {"acme": {"url": "https://acme.test", "key": "acme-secret"}}
    assert "acme-secret" not in repr(settings)
    assert "acme-secret" not in settings.model_dump_json()


def test_manager_takes_its_token_from_one_file(configuration, secrets):
    configuration.write_text(MANAGER)
    (secrets / "manager.token").write_text("manager-secret")

    settings = Settings()

    assert settings.manager.name == "Druks Cloud"
    assert settings.manager.issuer == "https://portal.test"
    assert settings.manager.audience == INSTANCE_ID
    assert settings.manager.services == ["github"]
    assert settings.manager.token.get_secret_value() == "manager-secret"
    assert "manager-secret" not in repr(settings)
    assert "manager-secret" not in settings.model_dump_json()


@pytest.mark.parametrize(
    "body, message",
    [
        (MANAGER + 'token = "manager-secret"', "manager.token is a secret"),
        ('[manager]\nname = "Druks Cloud"', "manager.jwks_url, manager.issuer"),
        (MANAGER, "manager.token"),
        ('managed_by = "Druks Cloud"', "managed_by is now the"),
    ],
)
def test_an_incomplete_manager_refuses_to_start(configuration, secrets, body, message):
    configuration.write_text(body)

    with pytest.raises(ValueError, match=message) as error:
        Settings()

    assert "manager-secret" not in str(error.value)


async def test_the_manager_names_only_declared_services(configuration, secrets, druks_db):
    configuration.write_text(MANAGER.replace('["github"]', '["gihub"]'))
    (secrets / "manager.token").write_text("manager-secret")

    with pytest.raises(ServiceConnectError, match="gihub"):
        await Service.sync_configuration(druks_db)


async def test_a_configured_service_keeps_its_own_verifier_and_oauth_client(
    declared_services, declared_webhooks, configuration, secrets, druks_db, tmp_path
):
    class Acme(Service):
        authorization_endpoint = "https://acme.test/authorize"
        token_endpoint = "https://acme.test/token"

        class Settings(BaseModel):
            client_id: str
            client_secret: SecretStr

    class AcmeEvents(Webhook):
        provider = "acme"
        category = "events"

        async def request_is_authentic(self) -> bool:
            return self.request.headers.get("x-acme-signature") == "acme-signed"

        def get_action(self) -> str:
            return "ping"

    configuration.write_text(MANAGER + '[services.acme]\nclient_id = "id-1"')
    (secrets / "manager.token").write_text("manager-secret")
    (secrets / "services.acme.client_secret").write_text("acme-secret")
    bind_ambient_session(druks_db)
    await Service.sync_configuration(druks_db)

    client = await Acme.get_oauth_client()
    assert client.token_endpoint == "https://acme.test/token"
    assert client.client_secret == "acme-secret"
    assert not client.token_headers

    api = configure_app_for_test(settings=Settings(data_dir=tmp_path))
    async with asgi_client(api) as http:
        refused = await http.post("/_external/acme/events/", content=b"{}")
        accepted = await http.post(
            "/_external/acme/events/", content=b"{}", headers={"X-Acme-Signature": "acme-signed"}
        )
    assert refused.status_code == 401
    assert accepted.json() == {"accepted": True, "handled": False}


@pytest.mark.parametrize(
    "body, secret, message",
    [
        ('[services.acme]\nurl = "https://acme.test"', "invalid-secret", "services.acme.key"),
        ('[services.acme]\nurl = ["sensitive-value"]', "safe-key", "services.acme.url"),
        ("[services.acme]", "safe-key", "services.acme.url"),
        ('[services.unknown]\nurl = "sensitive-value"', "safe-key", "no installed app"),
    ],
)
async def test_invalid_service_entry_stops_the_sync_without_its_values(
    acme, configuration, secrets, druks_db, caplog, body, secret, message
):
    configuration.write_text(body)
    (secrets / "services.acme.key").write_text(secret)
    with pytest.raises(ServiceConnectError, match=message) as error:
        await Service.sync_configuration(druks_db)
    output = "".join(traceback.format_exception(error.value)) + caplog.text
    assert "invalid-secret" not in output
    assert "sensitive-value" not in output


async def test_sync_keeps_the_vault_id_and_replaces_credentials(
    acme, configuration, secrets, druks_db
):
    bind_ambient_session(druks_db)
    original = await connect_service(
        "acme", identity={"url": "https://old.test"}, secrets={"key": "old-key"}
    )
    original_id = original.id
    configuration.write_text('[services.acme]\nurl = "https://acme.test"')
    (secrets / "services.acme.key").write_text("configured-key")
    for _ in range(2):
        await Service.sync_configuration(druks_db)
        row = await acme.get()
        assert row.id == original_id
        assert row.identity == {"url": "https://acme.test"}
        assert row.secrets == {"key": "configured-key"}
        assert await row.issue_token("", name="acme_key") == ("configured-key", None)

    (secrets / "services.acme.key").write_text("rotated-key")
    await Service.sync_configuration(druks_db)
    assert (await acme.get()).id == original_id
    assert (await acme.get()).secrets == {"key": "rotated-key"}
    rows = list(
        await druks_db.scalars(
            select(VaultSecret).where(VaultSecret.audience == Audience.service("acme"))
        )
    )
    assert len(rows) == 1

    configuration.write_text("")
    await Service.sync_configuration(druks_db)
    assert (await acme.get()).secrets == {"key": "rotated-key"}
    pasted = await acme.connect({"url": "https://self-hosted.test", "key": "pasted-key"})
    assert pasted.id == original_id


async def test_sync_keeps_the_facts_the_card_learned(acme, configuration, secrets, druks_db):
    bind_ambient_session(druks_db)
    configuration.write_text('[services.acme]\nurl = "https://acme.test"')
    (secrets / "services.acme.key").write_text("configured-key")
    await Service.sync_configuration(druks_db)
    card = await acme.get()
    card.identity = {**card.identity, "installations": ["acme", "paulo"]}
    await druks_db.flush()

    configuration.write_text('[services.acme]\nurl = "https://moved.test"')
    await Service.sync_configuration(druks_db)

    assert (await acme.get()).identity == {
        "url": "https://moved.test",
        "installations": ["acme", "paulo"],
    }


async def test_managed_card_is_read_only(acme, configuration, secrets, druks_db, tmp_path, caplog):
    configuration.write_text(MANAGER + '[services.acme]\nurl = "https://acme.test"')
    (secrets / "manager.token").write_text("manager-secret")
    (secrets / "services.acme.key").write_text("configured-key")
    settings = Settings(data_dir=tmp_path)
    bind_ambient_session(druks_db)
    await Service.sync_configuration(druks_db)
    api = configure_app_for_test(settings=settings)
    async with asgi_client(api) as client:
        response = await client.get("/api/services")
        [card] = [card for card in response.json() if card["slug"] == "acme"]
        assert card["connected"] and card["managed"]
        assert card["managedBy"] == "Druks Cloud"
        assert card["facts"] == {"url": "https://acme.test"}
        assert "configured-key" not in response.text
        for method, kwargs in (
            ("POST", {"json": {"url": "https://changed.test", "key": "other-key"}}),
            ("DELETE", {}),
        ):
            response = await client.request(method, "/api/services/acme", **kwargs)
            assert response.status_code == 409
            assert "configured-key" not in response.text
    with pytest.raises(ServiceManagedError):
        await acme.connect({"url": "https://changed.test", "key": "other-key"})
    assert (await acme.get()).secrets == {"key": "configured-key"}
    assert "configured-key" not in caplog.text


async def test_waha_link_uses_the_synced_card_and_keeps_the_session_key(
    configuration, secrets, monkeypatch, druks_db, caplog
):
    configuration.write_text(
        '[urls]\nendpoint = "https://instance.test"\n[services.waha]\nurl = "http://cloud.test/_instance/123/waha"'
    )
    (secrets / "services.waha.key").write_text("instance-key")
    bind_ambient_session(druks_db)
    await Service.sync_configuration(druks_db)
    owner = await Account.get_or_create(druks_db, "operator@example.com")
    requests = []
    send = httpx.AsyncClient.send

    async def waha_send(client, request, **kwargs):
        if request.url.host == "cloud.test":
            requests.append(request)
            body = {}
            if request.url.path.endswith("/api/sessions"):
                body = {"name": "123-session"}
            elif request.url.path.endswith("/api/keys"):
                body = {"id": "key-id", "key": "session-key"}
            return httpx.Response(200, request=request, json=body)
        return await send(client, request, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "send", waha_send)
    connection = await Waha.link(druks_db, owner, identity={})

    assert [request.method for request in requests] == ["POST", "POST", "PUT"]
    assert [request.headers["X-Api-Key"] for request in requests] == [
        "instance-key",
        "instance-key",
        "session-key",
    ]
    assert connection.secrets["key"] == "session-key"
    assert all(request.url.path.startswith("/_instance/123/waha/") for request in requests)
    assert (
        json.loads(requests[2].content)["config"]["webhooks"][0]["hmac"]["key"]
        == connection.secrets["webhook_secret"]
    )
    assert not any(
        secret in caplog.text
        for secret in ("instance-key", "session-key", connection.secrets["webhook_secret"])
    )

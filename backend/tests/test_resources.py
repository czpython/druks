import json
from datetime import UTC, datetime, timedelta
from hashlib import sha256

import httpx
import pytest
from conftest import bind_ambient_session, connect_service
from druks.sandbox import SandboxSecret
from druks.services import Resource, ResourceError, Service
from druks.services.resources import ManagedResource
from pydantic import BaseModel, SecretStr

MANAGER_TOKEN = "manager-secret"
CARD_URL = "https://manager.test/acme"
CONFIGURATION = f"""[manager]
name = "Manager"
jwks_url = "https://manager.test/.well-known/jwks.json"
issuer = "https://manager.test"
audience = "6f2c"
services = ["acme"]

[services.acme]
url = "{CARD_URL}"
resource_prefix = "6f2c-"
"""


class _Manager:
    def __init__(self) -> None:
        self.requests: list[tuple[str, str, dict | None]] = []
        self.authorizations: set[str] = set()
        self.lifetime = timedelta(hours=1)

    async def handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        self.requests.append((request.method, str(request.url), body))
        self.authorizations.add(request.headers["authorization"])
        if request.url.path.endswith("/token"):
            expires_at = datetime.now(UTC) + self.lifetime
            token = {"value": f"token-{len(self.requests)}", "expires_at": expires_at.isoformat()}
            return httpx.Response(201, request=request, json=token)
        if request.url.path.endswith("/domain") and request.method == "PUT":
            return httpx.Response(
                200, request=request, json={"domain": body["domain"], "state": "set"}
            )
        if request.method == "PUT":
            return httpx.Response(201, request=request, json={"name": "6f2c-site"})
        return httpx.Response(204, request=request)


@pytest.fixture
def acme(declared_services):
    class Acme(Service):
        host = "api.acme.test"

        class Settings(BaseModel):
            api_token: SecretStr

        class Workers(Resource):
            minted = 0

            async def get_token(self) -> tuple[str, datetime]:
                type(self).minted += 1
                return f"{self.name}-{self.minted}", datetime.now(UTC) + timedelta(hours=1)

        class Sites(Resource):
            def __init__(self, key: str) -> None:
                super().__init__(sha256(key.encode()).hexdigest()[:12])

    return Acme


@pytest.fixture
async def managed(acme, tmp_path, monkeypatch, druks_db):
    (tmp_path / "druks.toml").write_text(CONFIGURATION)
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "manager.token").write_text(MANAGER_TOKEN)
    monkeypatch.setenv("DRUKS_CONFIG", str(tmp_path / "druks.toml"))
    monkeypatch.setenv("DRUKS_SECRETS_DIR", str(secrets))
    bind_ambient_session(druks_db)
    await Service.sync_configuration(druks_db)


@pytest.fixture
def manager(monkeypatch):
    fake = _Manager()

    async def handle_async_request(transport, request):
        return await fake.handle(request)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", handle_async_request)
    return fake


async def test_a_pasted_card_runs_the_authors_resource(acme):
    worker = acme.Workers("site")

    assert type(worker) is acme.Workers
    assert worker.name == "site"


def test_a_resource_has_only_the_four_steps():
    with pytest.raises(TypeError, match="Workers.versions is not a step"):

        class Workers(Resource):
            async def versions(self): ...


async def test_a_resource_gives_the_cards_secret_by_default(acme, druks_db):
    bind_ambient_session(druks_db)
    await connect_service("acme", identity={}, secrets={"api_token": "pasted"})

    assert await acme.Sites("site-id").get_token() == ("pasted", None)


def test_a_resource_takes_its_slug_from_its_class_name():
    class DnsRecords(Resource):
        pass

    class Zones(Resource):
        slug = "zones_v2"

    assert (DnsRecords.slug, Zones.slug) == ("dns_records", "zones_v2")


async def test_a_managed_card_sends_each_step_to_its_url(acme, managed, manager):
    worker = acme.Workers("site")

    await worker.create()
    answer = await worker.update(domain="www.acme.com")
    removal = await worker.update(domain=None)
    value, _ = await worker.get_token()
    await worker.delete()

    assert isinstance(worker, ManagedResource)
    assert worker.name == "6f2c-site"
    assert answer == {"domain": "www.acme.com", "state": "set"}
    assert removal is None
    assert value == "token-4"
    assert manager.requests == [
        ("PUT", f"{CARD_URL}/workers/6f2c-site", None),
        ("PUT", f"{CARD_URL}/workers/6f2c-site/domain", {"domain": "www.acme.com"}),
        ("DELETE", f"{CARD_URL}/workers/6f2c-site/domain", None),
        ("POST", f"{CARD_URL}/workers/6f2c-site/token", None),
        ("DELETE", f"{CARD_URL}/workers/6f2c-site", None),
    ]
    assert manager.authorizations == {f"Bearer {MANAGER_TOKEN}"}


async def test_a_missing_managed_resource_counts_as_deleted(acme, managed, manager, monkeypatch):
    async def missing(request):
        return httpx.Response(404, request=request)

    monkeypatch.setattr(manager, "handle", missing)

    await acme.Workers("site").delete()


async def test_a_refused_step_raises_a_resource_error(acme, managed, manager, monkeypatch):
    async def refuse(request):
        return httpx.Response(403, request=request, json={"message": "Not your Worker."})

    monkeypatch.setattr(manager, "handle", refuse)

    with pytest.raises(
        ResourceError, match="create on workers/6f2c-site failed: 403 Not your"
    ) as refused:
        await acme.Workers("site").create()

    assert (refused.value.status_code, refused.value.reason) == (403, "Not your Worker.")


@pytest.mark.parametrize(
    "lifetime, values",
    [(timedelta(hours=1), ["token-1", "token-1"]), (timedelta(seconds=30), ["token-1", "token-2"])],
)
async def test_a_token_is_reused_until_shortly_before_it_expires(
    acme, managed, manager, lifetime, values
):
    manager.lifetime = lifetime

    tokens = [await acme.Workers("site").get_token() for _ in values]

    assert [value for value, _ in tokens] == values


async def test_a_pasted_cards_token_is_reused_too(acme):
    tokens = [await acme.Workers("site").get_token() for _ in range(2)]

    assert [value for value, _ in tokens] == ["site-1", "site-1"]


async def test_a_box_rebuilds_the_resource_from_its_key(acme, managed, manager):
    card = await acme.get()
    site = acme.Sites("site-id")

    [secret] = await site.get_secrets()
    value, _ = await card.issue_token(secret.resources, name=secret.name)

    assert secret == SandboxSecret(
        name="acme_api_token", secret_id=card.id, resources=("sites/site-id",), host="api.acme.test"
    )
    assert site.name == f"6f2c-{sha256(b'site-id').hexdigest()[:12]}"
    assert value == "token-1"
    assert manager.requests == [("POST", f"{CARD_URL}/sites/{site.name}/token", None)]

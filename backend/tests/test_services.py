import hashlib
import hmac
import html
import json
from types import SimpleNamespace

import httpx
import pytest
from druks.core.apis.github import GitHubClient, get_github_client
from druks.core.webhooks.github import GitHubEvents
from druks.services.exceptions import ServiceNotConnectedError
from druks.services.models import ServiceIdentity
from druks.testing import make_settings
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import text

_PEM = "-----BEGIN RSA PRIVATE KEY-----\nline-one\nline-two\n-----END RSA PRIVATE KEY-----\n"
_SECRET = "hook-secret-value"


def _connect(
    *, app_id="12345", slug="druks-operator", private_key=_PEM, webhook_secret=_SECRET
) -> ServiceIdentity:
    return ServiceIdentity.connect(
        "github",
        identity={"app_id": app_id, "slug": slug},
        secrets={"private_key": private_key, "webhook_secret": webhook_secret},
    )


def _github_entry(client: TestClient) -> dict:
    entries = client.get("/api/services").json()
    return next(entry for entry in entries if entry["name"] == "github")


# --- The row ----------------------------------------------------------------


def test_secrets_round_trip_and_rest_is_ciphertext(druks_db):
    _connect()
    druks_db.expire_all()

    row = ServiceIdentity.get("github")
    assert row.identity["app_id"] == "12345"
    assert row.identity["slug"] == "druks-operator"
    assert row.connected_at is not None
    assert row.secrets["private_key"] == _PEM
    assert row.secrets["webhook_secret"] == _SECRET

    stored = druks_db.execute(text("SELECT secrets FROM service_identities")).scalar_one()
    assert _PEM.encode() not in bytes(stored)
    assert _SECRET.encode() not in bytes(stored)


def test_connect_replaces_the_single_github_row(druks_db):
    _connect()
    _connect(app_id="777", slug="new-slug", private_key="new-pem", webhook_secret="new-secret")
    druks_db.expire_all()

    count = druks_db.execute(text("SELECT count(*) FROM service_identities")).scalar_one()
    assert count == 1
    row = ServiceIdentity.get("github")
    assert row.identity["app_id"] == "777"
    assert row.identity["slug"] == "new-slug"
    assert row.secrets["private_key"] == "new-pem"
    assert row.secrets["webhook_secret"] == "new-secret"


def test_get_raises_when_the_service_is_not_connected(druks_db):
    with pytest.raises(ServiceNotConnectedError, match="github is not connected"):
        ServiceIdentity.get("github")


# --- The zero-argument client factory ---------------------------------------


def test_client_factory_resolves_only_the_row(druks_db):
    _connect()

    client = get_github_client()

    assert client._app_id == "12345"
    assert client._private_key == _PEM
    assert client._slug == "druks-operator"


async def test_mention_handle_is_the_stored_slug(druks_db):
    # No transport stub: a refetch would ask GitHub and fail loudly here.
    _connect()

    assert await get_github_client().get_mention_handle() == "druks-operator"


def test_client_factory_raises_the_typed_error_when_absent(druks_db):
    with pytest.raises(ServiceNotConnectedError):
        get_github_client()


# --- Webhook verification ----------------------------------------------------


def _events(body: bytes, signature: str | None, tmp_path) -> GitHubEvents:
    headers = {}
    if signature is not None:
        headers["x-hub-signature-256"] = signature
    events = GitHubEvents(
        request=SimpleNamespace(headers=headers),  # type: ignore[arg-type]
        kwargs={},
        settings=make_settings(tmp_path),
    )
    events.raw_body = body
    return events


def test_webhook_accepts_the_row_secrets_signature(druks_db, tmp_path):
    _connect()
    body = b'{"hello":"world"}'
    signature = "sha256=" + hmac.new(_SECRET.encode(), body, hashlib.sha256).hexdigest()

    assert _events(body, signature, tmp_path).request_is_authentic()


def test_webhook_rejects_a_mismatched_signature(druks_db, tmp_path):
    _connect()

    with pytest.raises(HTTPException) as raised:
        _events(b"{}", "sha256=bogus", tmp_path).request_is_authentic()

    assert raised.value.status_code == 401


def test_webhook_rejects_a_missing_identity_before_dispatch(druks_db, tmp_path):
    with pytest.raises(HTTPException) as raised:
        _events(b"{}", "sha256=anything", tmp_path).request_is_authentic()

    assert raised.value.status_code == 401
    assert "not connected" in raised.value.detail


# --- The identity-gated API ---------------------------------------------------


def _mock_authenticated_app(monkeypatch, slug: str = "druks-operator") -> None:
    async def _slug(self) -> str:
        return slug

    monkeypatch.setattr(GitHubClient, "get_authenticated_app_slug", _slug)


def test_list_reports_each_declared_service(druks_client: TestClient):
    entry = _github_entry(druks_client)

    assert entry["connected"] is False
    assert entry["required"] is True
    assert entry["facts"] == {}
    assert entry["connectedAt"] is None
    assert [field["name"] for field in entry["fields"]] == [
        "app_id",
        "private_key",
        "webhook_secret",
    ]
    assert [field["type"] for field in entry["fields"]] == ["str", "secret", "secret"]
    assert [field["label"] for field in entry["fields"]] == [
        "App ID",
        "Private key (PEM)",
        "Webhook secret",
    ]
    assert [field["multiline"] for field in entry["fields"]] == [False, True, False]


def test_post_authenticates_then_creates_the_row(druks_client: TestClient, druks_db, monkeypatch):
    _mock_authenticated_app(monkeypatch)

    response = druks_client.post(
        "/api/services/github",
        json={"app_id": "12345", "private_key": _PEM, "webhook_secret": _SECRET},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["connected"] is True
    assert body["facts"] == {"app_id": "12345", "slug": "druks-operator"}
    # No response carries either pasted secret.
    assert _PEM not in response.text
    assert _SECRET not in response.text

    connected = _github_entry(druks_client)
    assert connected["connected"] is True
    assert connected["facts"]["slug"] == "druks-operator"


def test_post_replaces_an_existing_row(druks_client: TestClient, druks_db, monkeypatch):
    _connect()
    _mock_authenticated_app(monkeypatch, slug="replacement-app")

    response = druks_client.post(
        "/api/services/github",
        json={"app_id": "777", "private_key": "new-pem", "webhook_secret": "new-secret"},
    )

    assert response.status_code == 200
    assert response.json()["facts"] == {"app_id": "777", "slug": "replacement-app"}


def test_post_rejects_an_unknown_service(druks_client: TestClient):
    response = druks_client.post("/api/services/nope", json={"anything": "x"})

    assert response.status_code == 404


def test_invalid_credentials_preserve_the_previous_row(
    druks_client: TestClient, druks_db, monkeypatch
):
    _connect()

    async def _rejected(self) -> str:
        raise RuntimeError("boom-marker bad credentials")

    monkeypatch.setattr(GitHubClient, "get_authenticated_app_slug", _rejected)

    response = druks_client.post(
        "/api/services/github",
        json={"app_id": "999", "private_key": "bad-pem", "webhook_secret": "bad-secret"},
    )

    assert response.status_code == 422
    assert "credentials" in response.json()["detail"]
    # The failure detail never echoes the submitted values or the raw error.
    assert "boom-marker" not in response.text
    assert "bad-pem" not in response.text

    druks_db.expire_all()
    row = ServiceIdentity.get("github")
    assert row.identity["app_id"] == "12345"
    assert row.secrets["private_key"] == _PEM


def test_blank_fields_are_rejected_without_touching_github(
    druks_client: TestClient, druks_db, monkeypatch
):
    async def _never(self) -> str:
        raise AssertionError("blank fields must not reach GitHub")

    monkeypatch.setattr(GitHubClient, "get_authenticated_app_slug", _never)

    response = druks_client.post(
        "/api/services/github",
        json={"app_id": " ", "private_key": "", "webhook_secret": ""},
    )

    assert response.status_code == 422
    with pytest.raises(ServiceNotConnectedError):
        ServiceIdentity.get("github")


# --- The manifest flow (dashboard-created App) -------------------------------


def _manifest_from(page: str) -> dict:
    value = page.partition('name="manifest" value="')[2].partition('">')[0]
    return json.loads(html.unescape(value))


def test_manifest_page_submits_the_documented_app_to_github(druks_client: TestClient, tmp_path):
    druks_client.app.state.settings = make_settings(
        tmp_path, urls={"endpoint": "https://druks.example/"}
    )

    response = druks_client.get("/api/core/github/manifest")

    assert response.status_code == 200
    assert 'action="https://github.com/settings/apps/new"' in response.text
    manifest = _manifest_from(response.text)
    assert manifest["name"] == "druks"
    assert manifest["url"] == "https://druks.example"
    assert manifest["redirect_url"] == "https://druks.example/api/core/github/manifest/callback"
    assert manifest["hook_attributes"] == {
        "url": "https://druks.example/_external/github/events/",
        "active": True,
    }
    assert manifest["public"] is False
    assert manifest["default_permissions"]["contents"] == "write"
    assert "pull_request_review" in manifest["default_events"]


def test_manifest_page_prefers_the_webhook_host_for_deliveries(druks_client: TestClient, tmp_path):
    druks_client.app.state.settings = make_settings(
        tmp_path,
        urls={"endpoint": "https://druks.example", "webhook_host": "hooks.druks.example"},
    )

    manifest = _manifest_from(druks_client.get("/api/core/github/manifest").text)

    assert (
        manifest["hook_attributes"]["url"] == "https://hooks.druks.example/_external/github/events/"
    )


def test_manifest_page_lets_the_operator_target_an_org(druks_client: TestClient, tmp_path):
    # The org lives on the page, not in the card: naming one reroutes the
    # form to that org's create URL, and the input itself never reaches GitHub.
    druks_client.app.state.settings = make_settings(
        tmp_path, urls={"endpoint": "https://druks.example"}
    )

    response = druks_client.get("/api/core/github/manifest")

    assert '<input name="org">' in response.text
    assert "https://github.com/organizations/" in response.text
    assert "this.elements.org.disabled = true" in response.text


def test_manifest_page_refuses_without_an_endpoint(druks_client: TestClient):
    response = druks_client.get("/api/core/github/manifest")

    assert response.status_code == 409
    assert "urls.endpoint" in response.json()["detail"]


def test_manifest_callback_exchanges_the_code_and_connects(
    druks_client: TestClient, druks_db, monkeypatch
):
    exchanged = []

    async def fake_post(self, url, **kwargs):
        exchanged.append(url)
        return httpx.Response(
            200,
            json={
                "id": 4242,
                "slug": "druks",
                "pem": _PEM,
                "webhook_secret": _SECRET,
                "html_url": "https://github.com/apps/druks",
            },
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    response = druks_client.get("/api/core/github/manifest/callback", params={"code": "fresh-code"})

    assert response.status_code == 200
    assert exchanged == ["https://api.github.com/app-manifests/fresh-code/conversions"]
    assert "https://github.com/apps/druks/installations/new" in response.text
    # The connect card listens per service on the shared channel.
    assert "BroadcastChannel('druks-service-connect')" in response.text
    assert 'postMessage("github")' in response.text
    # The page never carries the stored secrets.
    assert "line-one" not in response.text
    assert _SECRET not in response.text
    druks_db.expire_all()
    row = ServiceIdentity.get("github")
    assert row.identity == {"app_id": "4242", "slug": "druks"}
    assert row.secrets == {"private_key": _PEM, "webhook_secret": _SECRET}


def test_manifest_callback_rejects_a_dead_code(druks_client: TestClient, druks_db, monkeypatch):
    async def fake_post(self, url, **kwargs):
        return httpx.Response(404, json={"message": "Not Found"})

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    response = druks_client.get("/api/core/github/manifest/callback", params={"code": "stale"})

    assert response.status_code == 400
    assert "restart" in response.json()["detail"]
    with pytest.raises(ServiceNotConnectedError):
        ServiceIdentity.get("github")


# --- OAuth declaration --------------------------------------------------------


@pytest.fixture
def declared_services():
    # Service subclasses self-register at class definition; tests declare
    # inside this fixture and leave the registry as found.
    from druks.extensions.registry import services

    saved = dict(services._items)
    yield
    services._items.clear()
    services._items.update(saved)


def test_get_oauth_client_reads_the_connected_identity(declared_services, druks_db):
    from druks.services import Service
    from pydantic import BaseModel, SecretStr

    class Acme(Service):
        name = "acme"
        title = "Acme OAuth app"
        authorization_endpoint = "https://acme.test/authorize"
        token_endpoint = "https://acme.test/token"
        basic_auth = True

        class Settings(BaseModel):
            client_id: str
            client_secret: SecretStr

    ServiceIdentity.connect(
        "acme", identity={"client_id": "id-1"}, secrets={"client_secret": "sec-1"}
    )

    client = Acme.get_oauth_client()

    assert client.provider == "acme"
    assert client.authorization_endpoint == "https://acme.test/authorize"
    assert client.token_endpoint == "https://acme.test/token"
    assert client.client_id == "id-1"
    assert client.client_secret == "sec-1"
    assert client.basic_auth is True


def test_oauth_service_declarations_fail_loudly(declared_services):
    from druks.services import Service
    from pydantic import BaseModel, SecretStr

    with pytest.raises(TypeError, match="client_id and client_secret"):

        class Keyless(Service):
            name = "keyless"
            title = "Keyless"
            authorization_endpoint = "https://acme.test/authorize"
            token_endpoint = "https://acme.test/token"

            class Settings(BaseModel):
                api_key: SecretStr

    with pytest.raises(TypeError, match="both OAuth endpoints"):

        class HalfDeclared(Service):
            name = "half_declared"
            title = "Half declared"
            token_endpoint = "https://acme.test/token"

            class Settings(BaseModel):
                client_id: str
                client_secret: SecretStr

    class Plain(Service):
        name = "plain_service"
        title = "Plain"

        class Settings(BaseModel):
            api_key: SecretStr

    with pytest.raises(TypeError, match="no OAuth endpoints"):
        Plain.get_oauth_client()

import hashlib
import hmac
import html
import json
from types import SimpleNamespace
from urllib.parse import parse_qsl, urlparse

import httpx
import pytest
from druks.accounts.models import Account
from druks.core.apis.github import GitHubClient, get_github_client
from druks.core.webhooks.github import GitHubEvents
from druks.database import db_session
from druks.services.exceptions import ServiceNotConnectedError
from druks.services.models import OauthConnection, ServiceIdentity
from druks.services.oauth import OauthClient
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
    return next(entry for entry in entries if entry["slug"] == "github")


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
        authorization_endpoint = "https://acme.test/authorize"
        token_endpoint = "https://acme.test/token"
        basic_auth = True
        extra_authorize_params = {"access_type": "offline"}

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
    assert client.extra_authorize_params == {"access_type": "offline"}


def test_oauth_service_declarations_fail_loudly(declared_services):
    from druks.services import Service
    from pydantic import BaseModel, SecretStr

    with pytest.raises(TypeError, match="client_id and client_secret"):

        class Keyless(Service):
            authorization_endpoint = "https://acme.test/authorize"
            token_endpoint = "https://acme.test/token"

            class Settings(BaseModel):
                api_key: SecretStr

    with pytest.raises(TypeError, match="both OAuth endpoints"):

        class HalfDeclared(Service):
            token_endpoint = "https://acme.test/token"

            class Settings(BaseModel):
                client_id: str
                client_secret: SecretStr

    with pytest.raises(TypeError, match="keys by"):

        class Named(Service):
            name = "named"

            class Settings(BaseModel):
                api_key: SecretStr

    class Plain(Service):
        class Settings(BaseModel):
            api_key: SecretStr

    with pytest.raises(TypeError, match="no OAuth endpoints"):
        Plain.get_oauth_client()


def test_abstract_base_shares_declarations_without_registering(declared_services):
    from druks.extensions.registry import services
    from druks.services import Service
    from pydantic import BaseModel, SecretStr

    class AcmeBase(Service):
        abstract = True
        authorization_endpoint = "https://acme.test/authorize"
        token_endpoint = "https://acme.test/token"

        class Settings(BaseModel):
            client_id: str
            client_secret: SecretStr

    class Mail(AcmeBase):
        slug = "acme_mail"

    assert services.get("acme_mail") is Mail
    assert Mail.abstract is False
    assert Mail.settings_model is AcmeBase.Settings

    with pytest.raises(TypeError, match="abstract"):

        class Pinned(Service):
            abstract = True
            slug = "pinned"


def test_with_scopes_declares_the_union_and_reads_connections(declared_services, monkeypatch):
    from druks.services import Service
    from pydantic import BaseModel, SecretStr

    class Acme(Service):
        authorization_endpoint = "https://acme.test/authorize"
        token_endpoint = "https://acme.test/token"

        class Settings(BaseModel):
            client_id: str
            client_secret: SecretStr

    class NightWatch:
        name = "night_watch"
        acme = Acme.with_scopes("profile.read", "posts.write")

    class Digest:
        name = "digest"
        acme = Acme.with_scopes("profile.read")

    monkeypatch.setattr("druks.services.base.iter_extensions", lambda: [NightWatch, Digest])

    assert NightWatch.acme.scopes == ("profile.read", "posts.write")
    assert Acme.required_scopes() == ("posts.write", "profile.read")
    assert [declaration.label for declaration in Acme.declarations()] == [
        "night_watch.acme",
        "digest.acme",
    ]

    from druks.accounts.constants import SYSTEM_ACCOUNT_ID
    from druks.services.models import OauthConnection

    row = OauthConnection.create(
        provider="acme",
        account_id=SYSTEM_ACCOUNT_ID,
        refresh_token="rt-1",
        scopes=["profile.read"],
        identity={"email": "night@acme.test"},
    )
    connections = NightWatch.acme.list_for_account(SYSTEM_ACCOUNT_ID)
    assert [connection.id for connection in connections] == [row.id]
    assert connections[0].scopes == ["profile.read"]
    assert connections[0].identity == {"email": "night@acme.test"}
    assert connections[0].account_id == SYSTEM_ACCOUNT_ID
    assert NightWatch.acme.get(row.id).id == row.id
    assert not NightWatch.acme.get("missing")

    # The handle serves live connections only; the revoked row survives.
    row.revoke("user")
    assert not NightWatch.acme.list_for_account(SYSTEM_ACCOUNT_ID)
    assert not NightWatch.acme.get(row.id)
    assert OauthConnection.get(row.id).identity == {"email": "night@acme.test"}


async def test_get_identity_without_a_declared_endpoint_is_empty(declared_services):
    from druks.services import Service
    from pydantic import BaseModel, SecretStr

    class Quiet(Service):
        authorization_endpoint = "https://quiet.test/authorize"
        token_endpoint = "https://quiet.test/token"

        class Settings(BaseModel):
            client_id: str
            client_secret: SecretStr

    assert await Quiet.get_identity("at-1") == {}


def test_with_scopes_requires_oauth_endpoints(declared_services):
    from druks.services import Service
    from pydantic import BaseModel, SecretStr

    class Plain(Service):
        class Settings(BaseModel):
            api_key: SecretStr

    with pytest.raises(TypeError, match="no OAuth endpoints"):
        Plain.with_scopes("profile.read")


# --- The connect door: /api/oauth ------------------------------------------


@pytest.fixture
def acme(declared_services, monkeypatch):
    from druks.services import Service
    from pydantic import BaseModel, SecretStr

    class Acme(Service):
        authorization_endpoint = "https://acme.test/authorize"
        token_endpoint = "https://acme.test/token"
        identity_endpoint = "https://acme.test/whoami"
        identity_scopes = ("openid",)
        identity_key = "sub"

        class Settings(BaseModel):
            client_id: str
            client_secret: SecretStr

    class NightWatch:
        name = "night_watch"
        acme = Acme.with_scopes("profile.read", "posts.write")

    monkeypatch.setattr("druks.services.base.iter_extensions", lambda: [NightWatch])
    tokens = {
        "access_token": "at-1",
        "refresh_token": "rt-1",
        "expires_in": 3600,
        "scope": "profile.read posts.write",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/whoami":
            return httpx.Response(200, json={"email": "op@acme.test"})
        return httpx.Response(200, json=tokens)

    monkeypatch.setattr(
        "druks.services.oauth._http",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    return Acme


@pytest.fixture
def keyed_acme(acme, monkeypatch):
    async def get_identity(service, access_token):
        assert service is acme
        assert access_token == "at-1"
        return {"sub": "account-1", "email": "op@acme.test"}

    monkeypatch.setattr(acme, "get_identity", classmethod(get_identity))
    return acme


@pytest.fixture
def oauth_events(monkeypatch):
    events = []

    async def record(name, **kwargs):
        events.append((name, kwargs))

    monkeypatch.setattr("druks.services.routes.publish", record)
    return events


def _complete_oauth_sign_in(client: TestClient, provider: str = "acme") -> None:
    consent = client.get(f"/api/oauth/{provider}/connect", follow_redirects=False)
    state = dict(parse_qsl(urlparse(consent.headers["location"]).query))["state"]
    response = client.get("/api/oauth/callback", params={"state": state, "code": "c-1"})

    assert response.status_code == 200


def test_oauth_connect_redirects_to_consent_with_the_scope_union(
    tmp_path, acme, druks_db, monkeypatch
):
    from urllib.parse import parse_qsl, urlparse

    from druks.testing import configure_app_for_test

    ServiceIdentity.connect(
        "acme", identity={"client_id": "id-1"}, secrets={"client_secret": "sec-1"}
    )
    settings = make_settings(tmp_path, urls={"endpoint": "https://druks.example"})
    with TestClient(configure_app_for_test(settings=settings)) as client:
        response = client.get("/api/oauth/acme/connect", follow_redirects=False)

    assert response.status_code == 307
    consent = urlparse(response.headers["location"])
    params = dict(parse_qsl(consent.query))
    assert response.headers["location"].startswith("https://acme.test/authorize?")
    assert params["scope"] == "openid posts.write profile.read"
    assert params["redirect_uri"] == "https://druks.example/api/oauth/callback"


def test_oauth_connect_guards(tmp_path, acme, druks_db):
    from druks.testing import configure_app_for_test

    with TestClient(configure_app_for_test(settings=make_settings(tmp_path))) as client:
        assert client.get("/api/oauth/github/connect").status_code == 404
        # No urls.endpoint configured.
        assert client.get("/api/oauth/acme/connect").status_code == 409

    settings = make_settings(tmp_path, urls={"endpoint": "https://druks.example"})
    with TestClient(configure_app_for_test(settings=settings)) as client:
        # The client credentials are not connected yet.
        response = client.get("/api/oauth/acme/connect", follow_redirects=False)
        assert response.status_code == 409
        assert "not connected" in response.json()["detail"]


async def test_oauth_callback_creates_and_reconnects_a_connection(
    tmp_path, acme, druks_db, monkeypatch
):
    from urllib.parse import parse_qsl, urlparse

    import druks.redis
    from druks.redis import close_client, get_client
    from druks.services.models import OauthConnection
    from druks.testing import configure_app_for_test

    ServiceIdentity.connect(
        "acme", identity={"client_id": "id-1"}, secrets={"client_secret": "sec-1"}
    )
    published = []

    async def record(name, **kwargs):
        published.append((name, kwargs))

    monkeypatch.setattr("druks.services.routes.publish", record)
    await close_client()
    settings = make_settings(tmp_path, urls={"endpoint": "https://druks.example"})
    with TestClient(configure_app_for_test(settings=settings)) as client:
        consent = client.get("/api/oauth/acme/connect", follow_redirects=False).headers["location"]
        state = dict(parse_qsl(urlparse(consent).query))["state"]
        page = client.get("/api/oauth/callback", params={"state": state, "code": "c-1"})
        assert page.status_code == 200
        assert "BroadcastChannel('druks-service-connect')" in page.text
        # The state is single-use; a denied consent lands loudly.
        assert (
            client.get("/api/oauth/callback", params={"state": state, "code": "c-1"}).status_code
            == 400
        )
        assert (
            client.get(
                "/api/oauth/callback", params={"state": "s", "code": "c", "error": "denied"}
            ).status_code
            == 400
        )

        [connection] = OauthConnection.list_for_provider("acme")
        assert connection.refresh_token.decrypt() == "rt-1"
        assert connection.identity == {"email": "op@acme.test"}
        assert connection.scopes == ["profile.read", "posts.write"]

        # Reconsent through the same connection replaces its tokens and
        # evicts the stale cached access token.
        stale_key = f"acme:access_token:{connection.id}"
        client.get("/api/oauth/acme/connect?connection=zzz", follow_redirects=False)
        reconnect = client.get(
            f"/api/oauth/acme/connect?connection={connection.id}", follow_redirects=False
        )
        state = dict(parse_qsl(urlparse(reconnect.headers["location"]).query))["state"]
        finish = client.get("/api/oauth/callback", params={"state": state, "code": "c-2"})
        assert finish.status_code == 200
        assert len(OauthConnection.list_for_provider("acme")) == 1

    assert [name for name, _ in published] == ["oauth.connected", "oauth.connected"]
    fresh, reconsent = (kwargs for _, kwargs in published)
    assert fresh == {
        "provider": "acme",
        "connection_id": connection.id,
        "account_id": connection.account_id,
        "reconsent": False,
    }
    assert reconsent["reconsent"] is True
    assert reconsent["connection_id"] == connection.id
    druks.redis._client = None
    assert not await get_client().get(stale_key)


def test_oauth_connect_rejects_an_unknown_reconnect_target(tmp_path, acme, druks_db):
    from druks.testing import configure_app_for_test

    ServiceIdentity.connect(
        "acme", identity={"client_id": "id-1"}, secrets={"client_secret": "sec-1"}
    )
    settings = make_settings(tmp_path, urls={"endpoint": "https://druks.example"})
    with TestClient(configure_app_for_test(settings=settings)) as client:
        assert client.get("/api/oauth/acme/connect?connection=zzz").status_code == 404


def test_fresh_sign_in_with_matching_identity_resurrects_revoked_connection(
    tmp_path, keyed_acme, druks_db, oauth_events
):
    from druks.testing import configure_app_for_test

    ServiceIdentity.connect(
        keyed_acme.slug, identity={"client_id": "id-1"}, secrets={"client_secret": "sec-1"}
    )
    account = Account.get_or_create("op@example.com")
    connection = OauthConnection.create(
        provider=keyed_acme.slug,
        account_id=account.id,
        refresh_token="rt-old",
        scopes=["profile.read"],
        identity={"sub": "account-1"},
    )
    connection_id = connection.id
    connection.revoke("user")
    settings = make_settings(tmp_path, urls={"endpoint": "https://druks.example"})

    with TestClient(configure_app_for_test(settings=settings)) as client:
        _complete_oauth_sign_in(client, keyed_acme.slug)

    db_session().expire_all()
    resurrected = OauthConnection.get(connection_id)
    assert resurrected
    assert not resurrected.revoked_at
    assert not resurrected.revoked_reason
    assert resurrected.refresh_token.decrypt() == "rt-1"
    assert [row.id for row in OauthConnection.list_for_provider(keyed_acme.slug)] == [connection_id]
    assert len(OauthConnection.list_for_provider(keyed_acme.slug, include_revoked=True)) == 1
    assert oauth_events == [
        (
            "oauth.connected",
            {
                "provider": keyed_acme.slug,
                "connection_id": connection_id,
                "account_id": account.id,
                "reconsent": True,
            },
        )
    ]


def test_matching_fresh_sign_in_lands_on_live_connection_and_evicts_cached_token(
    tmp_path, keyed_acme, druks_db, oauth_events, monkeypatch
):
    from druks.testing import configure_app_for_test

    ServiceIdentity.connect(
        keyed_acme.slug, identity={"client_id": "id-1"}, secrets={"client_secret": "sec-1"}
    )
    account = Account.get_or_create("op@example.com")
    connection = OauthConnection.create(
        provider=keyed_acme.slug,
        account_id=account.id,
        refresh_token="rt-old",
        scopes=["profile.read"],
        identity={"sub": "account-1"},
    )
    connection_id = connection.id
    evicted_connection_ids = []

    async def record_eviction(oauth_client, evicted_connection_id):
        assert oauth_client.provider == keyed_acme.slug
        evicted_connection_ids.append(evicted_connection_id)

    monkeypatch.setattr(OauthClient, "evict_access_token", record_eviction)
    settings = make_settings(tmp_path, urls={"endpoint": "https://druks.example"})

    with TestClient(configure_app_for_test(settings=settings)) as client:
        _complete_oauth_sign_in(client, keyed_acme.slug)

    db_session().expire_all()
    reconnected = OauthConnection.get(connection_id)
    assert reconnected
    assert reconnected.refresh_token.decrypt() == "rt-1"
    assert len(OauthConnection.list_for_provider(keyed_acme.slug, include_revoked=True)) == 1
    assert evicted_connection_ids == [connection_id]
    assert oauth_events[-1][1]["connection_id"] == connection_id
    assert oauth_events[-1][1]["reconsent"] is True


def test_fresh_sign_in_with_live_and_revoked_identity_matches_lands_on_live_connection(
    tmp_path, keyed_acme, druks_db, oauth_events
):
    from druks.testing import configure_app_for_test

    ServiceIdentity.connect(
        keyed_acme.slug, identity={"client_id": "id-1"}, secrets={"client_secret": "sec-1"}
    )
    account = Account.get_or_create("op@example.com")
    live = OauthConnection.create(
        provider=keyed_acme.slug,
        account_id=account.id,
        refresh_token="rt-live-old",
        scopes=["profile.read"],
        identity={"sub": "account-1"},
    )
    live_id = live.id
    revoked = OauthConnection.create(
        provider=keyed_acme.slug,
        account_id=account.id,
        refresh_token="rt-revoked-old",
        scopes=["profile.read"],
        identity={"sub": "account-1"},
    )
    revoked_id = revoked.id
    revoked.revoke("user")
    settings = make_settings(tmp_path, urls={"endpoint": "https://druks.example"})

    with TestClient(configure_app_for_test(settings=settings)) as client:
        _complete_oauth_sign_in(client, keyed_acme.slug)

    db_session().expire_all()
    reconnected = OauthConnection.get(live_id)
    still_revoked = OauthConnection.get(revoked_id)
    assert reconnected
    assert still_revoked
    assert reconnected.refresh_token.decrypt() == "rt-1"
    assert still_revoked.revoked_at
    assert not still_revoked.refresh_token
    assert len(OauthConnection.list_for_provider(keyed_acme.slug, include_revoked=True)) == 2
    assert oauth_events[-1][1]["connection_id"] == live_id
    assert oauth_events[-1][1]["reconsent"] is True


def test_fresh_sign_in_without_the_declared_identity_fact_creates_a_new_connection(
    tmp_path, acme, druks_db, oauth_events
):
    from druks.testing import configure_app_for_test

    ServiceIdentity.connect(
        acme.slug, identity={"client_id": "id-1"}, secrets={"client_secret": "sec-1"}
    )
    account = Account.get_or_create("op@example.com")
    revoked = OauthConnection.create(
        provider=acme.slug,
        account_id=account.id,
        refresh_token="rt-old",
        scopes=["profile.read"],
        identity={"sub": "account-1"},
    )
    revoked_id = revoked.id
    revoked.revoke("user")
    settings = make_settings(tmp_path, urls={"endpoint": "https://druks.example"})

    with TestClient(configure_app_for_test(settings=settings)) as client:
        _complete_oauth_sign_in(client, acme.slug)

    db_session().expire_all()
    [created] = OauthConnection.list_for_provider(acme.slug)
    assert created.id != revoked_id
    assert len(OauthConnection.list_for_provider(acme.slug, include_revoked=True)) == 2
    assert OauthConnection.get(revoked_id).revoked_at
    assert oauth_events[-1][1]["connection_id"] == created.id
    assert oauth_events[-1][1]["reconsent"] is False


def test_fresh_sign_in_after_revoke_creates_a_new_connection(tmp_path, acme, druks_db, monkeypatch):
    from urllib.parse import parse_qsl, urlparse

    from druks.services.models import OauthConnection
    from druks.testing import configure_app_for_test

    ServiceIdentity.connect(
        "acme", identity={"client_id": "id-1"}, secrets={"client_secret": "sec-1"}
    )
    published = []

    async def record(name, **kwargs):
        published.append((name, kwargs))

    monkeypatch.setattr("druks.services.routes.publish", record)
    settings = make_settings(tmp_path, urls={"endpoint": "https://druks.example"})
    with TestClient(configure_app_for_test(settings=settings)) as client:

        def sign_in():
            consent = client.get("/api/oauth/acme/connect", follow_redirects=False)
            state = dict(parse_qsl(urlparse(consent.headers["location"]).query))["state"]
            assert (
                client.get(
                    "/api/oauth/callback", params={"state": state, "code": "c-1"}
                ).status_code
                == 200
            )

        sign_in()
        [first] = OauthConnection.list_for_provider("acme")
        assert client.delete(f"/api/oauth/connections/{first.id}").status_code == 204

        # The identity facts do not include "sub", so the sign-in cannot
        # match the existing row. The revoked row stays as history.
        sign_in()
        [live] = OauthConnection.list_for_provider("acme")
        assert live.id != first.id
        assert len(OauthConnection.list_for_provider("acme", include_revoked=True)) == 2

    events = [name for name, _ in published]
    assert events == ["oauth.connected", "oauth.disconnected", "oauth.connected"]
    assert published[-1][1] == {
        "provider": "acme",
        "connection_id": live.id,
        "account_id": live.account_id,
        "reconsent": False,
    }


def test_reconsent_returns_a_revoked_connection_to_life(tmp_path, acme, druks_db, monkeypatch):
    from urllib.parse import parse_qsl, urlparse

    from druks.services.models import OauthConnection
    from druks.testing import configure_app_for_test

    ServiceIdentity.connect(
        "acme", identity={"client_id": "id-1"}, secrets={"client_secret": "sec-1"}
    )
    published = []

    async def record(name, **kwargs):
        published.append((name, kwargs))

    monkeypatch.setattr("druks.services.routes.publish", record)
    settings = make_settings(tmp_path, urls={"endpoint": "https://druks.example"})
    with TestClient(configure_app_for_test(settings=settings)) as client:
        consent = client.get("/api/oauth/acme/connect", follow_redirects=False)
        state = dict(parse_qsl(urlparse(consent.headers["location"]).query))["state"]
        client.get("/api/oauth/callback", params={"state": state, "code": "c-1"})
        [connection] = OauthConnection.list_for_provider("acme")
        assert client.delete(f"/api/oauth/connections/{connection.id}").status_code == 204

        # Reconsent names the row and makes the revoked consent live again.
        reconnect = client.get(
            f"/api/oauth/acme/connect?connection={connection.id}", follow_redirects=False
        )
        state = dict(parse_qsl(urlparse(reconnect.headers["location"]).query))["state"]
        assert (
            client.get("/api/oauth/callback", params={"state": state, "code": "c-2"}).status_code
            == 200
        )

        # The routes wrote in their own transactions; drop stale instances.
        db_session().expire_all()
        [live] = OauthConnection.list_for_provider("acme")
        assert live.id == connection.id
        assert not live.revoked_at
        assert not live.revoked_reason
        assert live.refresh_token.decrypt() == "rt-1"

    assert published[-1][1] == {
        "provider": "acme",
        "connection_id": live.id,
        "account_id": live.account_id,
        "reconsent": True,
    }


def test_connections_list_and_revoke(tmp_path, acme, druks_db, monkeypatch):
    from druks.accounts.models import Account
    from druks.services.models import OauthConnection
    from druks.testing import configure_app_for_test

    published = []

    async def record(name, **kwargs):
        published.append((name, kwargs))

    monkeypatch.setattr("druks.services.routes.publish", record)
    me = Account.get_or_create("op@example.com")
    with TestClient(configure_app_for_test(settings=make_settings(tmp_path))) as client:
        row = OauthConnection.create(
            provider="acme", account_id=me.id, refresh_token="rt-1", scopes=["profile.read"]
        )
        [listed] = client.get("/api/oauth/connections").json()
        assert listed["id"] == row.id
        assert listed["provider"] == "acme"
        assert listed["scopes"] == ["profile.read"]
        assert listed["revokedAt"] is None

        assert client.delete(f"/api/oauth/connections/{row.id}").status_code == 204
        assert not OauthConnection.list_for_provider("acme")
        # The route revoked in its own transaction; drop the stale instance.
        db_session().expire_all()
        revoked = OauthConnection.get(row.id)
        assert revoked.revoked_at
        assert revoked.revoked_reason == "user"
        assert not revoked.refresh_token
        # The audit read keeps serving the revoked row as history.
        [listed] = client.get("/api/oauth/connections").json()
        assert listed["revokedAt"]
        assert listed["revokedReason"] == "user"
        # Revoking is idempotent: the second delete finds the state true.
        assert client.delete(f"/api/oauth/connections/{row.id}").status_code == 204

    assert published == [
        (
            "oauth.disconnected",
            {"provider": "acme", "connection_id": row.id, "account_id": me.id},
        )
    ]


def test_replacing_the_client_credentials_revokes_its_connections(
    tmp_path, acme, druks_db, monkeypatch
):
    from druks.accounts.constants import SYSTEM_ACCOUNT_ID
    from druks.services.models import OauthConnection
    from druks.testing import configure_app_for_test

    published = []

    async def record(name, **kwargs):
        published.append((name, kwargs))

    monkeypatch.setattr("druks.services.routes.publish", record)
    row = OauthConnection.create(
        provider="acme", account_id=SYSTEM_ACCOUNT_ID, refresh_token="rt-old", scopes=[]
    )

    with TestClient(configure_app_for_test(settings=make_settings(tmp_path))) as client:
        response = client.post(
            "/api/services/acme", json={"client_id": "id-2", "client_secret": "sec-2"}
        )
        assert response.status_code == 200

    # The new client can never refresh the old client's connections.
    db_session().expire_all()
    assert not OauthConnection.list_for_provider("acme")
    [revoked] = OauthConnection.list_for_provider("acme", include_revoked=True)
    assert revoked.id == row.id
    assert revoked.revoked_reason == "client_replaced"
    assert not revoked.refresh_token
    assert published == [
        (
            "oauth.disconnected",
            {"provider": "acme", "connection_id": row.id, "account_id": SYSTEM_ACCOUNT_ID},
        )
    ]


def test_list_serves_the_connections_beside_the_declared_union(tmp_path, acme, druks_db):
    from druks.accounts.constants import SYSTEM_ACCOUNT_ID
    from druks.services.models import OauthConnection
    from druks.testing import configure_app_for_test

    def entry(client, slug="acme"):
        return next(e for e in client.get("/api/services").json() if e["slug"] == slug)

    with TestClient(configure_app_for_test(settings=make_settings(tmp_path))) as client:
        assert entry(client, "github")["isOauth"] is False
        before = entry(client)
        assert before["isOauth"] is True
        assert before["connections"] == []
        assert before["requiredScopes"] == ["openid", "posts.write", "profile.read"]
        assert before["usedBy"] == ["night_watch.acme"]

        row = OauthConnection.create(
            provider="acme",
            account_id=SYSTEM_ACCOUNT_ID,
            refresh_token="rt-1",
            scopes=["profile.read"],
        )
        [connection] = entry(client)["connections"]
        assert connection["id"] == row.id
        assert connection["scopes"] == ["profile.read"]
        assert connection["identity"] == {}
        assert connection["connectedAt"]


def test_next_lands_the_user_back_on_the_extension_page(tmp_path, acme, druks_db):
    from urllib.parse import parse_qsl, urlparse

    from druks.testing import configure_app_for_test

    ServiceIdentity.connect(
        "acme", identity={"client_id": "id-1"}, secrets={"client_secret": "sec-1"}
    )
    settings = make_settings(tmp_path, urls={"endpoint": "https://druks.example"})
    with TestClient(configure_app_for_test(settings=settings)) as client:
        consent = client.get(
            "/api/oauth/acme/connect?next=/app/night_watch/accounts",
            follow_redirects=False,
        ).headers["location"]
        state = dict(parse_qsl(urlparse(consent).query))["state"]

        finish = client.get(
            "/api/oauth/callback", params={"state": state, "code": "c-1"}, follow_redirects=False
        )

        assert finish.status_code == 307
        assert finish.headers["location"] == "/app/night_watch/accounts"


def test_next_rejects_anything_but_a_bare_path(tmp_path, acme, druks_db):
    from druks.testing import configure_app_for_test

    ServiceIdentity.connect(
        "acme", identity={"client_id": "id-1"}, secrets={"client_secret": "sec-1"}
    )
    settings = make_settings(tmp_path, urls={"endpoint": "https://druks.example"})
    with TestClient(configure_app_for_test(settings=settings)) as client:
        for hostile in ("https://evil.test/x", "//evil.test/x", "/\\evil.test", "app/page"):
            response = client.get(
                "/api/oauth/acme/connect", params={"next": hostile}, follow_redirects=False
            )
            assert response.status_code == 422, hostile

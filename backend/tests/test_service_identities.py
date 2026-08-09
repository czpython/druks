import hashlib
import hmac
from types import SimpleNamespace

import pytest
from druks.core.apis.github import GitHubClient, get_github_client
from druks.core.webhooks.github import GitHubEvents
from druks.service_identities.exceptions import ServiceNotConnectedError
from druks.service_identities.models import ServiceIdentity
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


# --- The row (AC1) ----------------------------------------------------------


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


# --- The zero-argument client factory (AC3) ---------------------------------


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


# --- Webhook verification (AC4) ---------------------------------------------


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


# --- The identity-gated API (AC2) -------------------------------------------


def _mock_authenticated_app(monkeypatch, slug: str = "druks-operator") -> None:
    async def _slug(self) -> str:
        return slug

    monkeypatch.setattr(GitHubClient, "get_authenticated_app_slug", _slug)


def test_get_reports_disconnected_state(druks_client: TestClient):
    body = druks_client.get("/api/service-identities/github").json()

    assert body == {"connected": False, "appId": None, "slug": None, "connectedAt": None}


def test_post_authenticates_then_creates_the_row(druks_client: TestClient, druks_db, monkeypatch):
    _mock_authenticated_app(monkeypatch)

    response = druks_client.post(
        "/api/service-identities/github",
        json={"appId": "12345", "privateKey": _PEM, "webhookSecret": _SECRET},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["connected"] is True
    assert body["appId"] == "12345"
    assert body["slug"] == "druks-operator"
    # No response carries either pasted secret.
    assert _PEM not in response.text
    assert _SECRET not in response.text

    connected = druks_client.get("/api/service-identities/github").json()
    assert connected["connected"] is True
    assert connected["slug"] == "druks-operator"


def test_post_replaces_an_existing_row(druks_client: TestClient, druks_db, monkeypatch):
    _connect()
    _mock_authenticated_app(monkeypatch, slug="replacement-app")

    response = druks_client.post(
        "/api/service-identities/github",
        json={"appId": "777", "privateKey": "new-pem", "webhookSecret": "new-secret"},
    )

    assert response.status_code == 200
    assert response.json()["appId"] == "777"
    assert response.json()["slug"] == "replacement-app"


def test_invalid_credentials_preserve_the_previous_row(
    druks_client: TestClient, druks_db, monkeypatch
):
    _connect()

    async def _rejected(self) -> str:
        raise RuntimeError("boom-marker bad credentials")

    monkeypatch.setattr(GitHubClient, "get_authenticated_app_slug", _rejected)

    response = druks_client.post(
        "/api/service-identities/github",
        json={"appId": "999", "privateKey": "bad-pem", "webhookSecret": "bad-secret"},
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
        "/api/service-identities/github",
        json={"appId": " ", "privateKey": "", "webhookSecret": ""},
    )

    assert response.status_code == 422
    with pytest.raises(ServiceNotConnectedError):
        ServiceIdentity.get("github")

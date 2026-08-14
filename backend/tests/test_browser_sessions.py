import base64

import pytest
from druks.accounts.dependencies import current_account, current_session_account
from druks.accounts.models import Account, PersonalAccessToken
from druks.browser import routes
from druks.browser.enums import BrowserSessionPayloadFormat, BrowserSessionStatus
from druks.browser.models import BrowserSession
from druks.database import db_session
from druks.secrets import utils as secret_utils
from druks.secrets.exceptions import SecretDecryptError
from druks.testing import configure_app_for_test, make_settings
from fastapi.testclient import TestClient
from sqlalchemy import text


@pytest.fixture
def client(tmp_path, druks_db, monkeypatch):
    settings = make_settings(tmp_path)
    monkeypatch.setattr(secret_utils, "load_settings", lambda: settings)
    app = configure_app_for_test(settings=settings)
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        for dependency in (current_account, current_session_account):
            app.dependency_overrides.pop(dependency, None)


def create_session(name: str = "x-main") -> BrowserSession:
    return BrowserSession.create(
        name=name,
        payload_format=BrowserSessionPayloadFormat.STORAGE_STATE,
        site="x.com",
    )


def test_import_survives_restart_and_delete_removes_the_row(client, tmp_path, monkeypatch):
    created = client.post(
        "/api/browser-sessions",
        json={"name": "x-main", "payloadFormat": "storage_state", "site": "x.com"},
    )
    assert created.status_code == 200
    assert created.json()["status"] == BrowserSessionStatus.NEEDS_LOGIN

    payload = b'{"cookies":[{"name":"auth_token","value":"secret"}],"origins":[]}'
    session_id = created.json()["id"]
    uploaded = client.put(
        f"/api/browser-sessions/{session_id}/state",
        content=payload,
        headers={"Content-Type": "application/octet-stream"},
    )
    assert uploaded.status_code == 200
    assert uploaded.json()["status"] == BrowserSessionStatus.READY
    assert uploaded.json()["lastRefreshedAt"]

    stored = (
        db_session()
        .execute(
            text("SELECT payload FROM browser_sessions WHERE id = :id"),
            {"id": session_id},
        )
        .scalar_one()
    )
    assert payload not in bytes(stored)
    with pytest.raises(SecretDecryptError):
        secret_utils.decrypt(bytes(stored), "another_table.payload")

    browser_session = BrowserSession.get_for_id(session_id)
    assert browser_session.payload.decrypt() == payload

    wrong_key = base64.b64encode(b"1" * 32).decode()
    wrong_settings = make_settings(tmp_path / "wrong", secrets={"secrets_key": wrong_key})
    with monkeypatch.context() as patch:
        patch.setattr(secret_utils, "load_settings", lambda: wrong_settings)
        with pytest.raises(SecretDecryptError):
            browser_session.payload.decrypt()

    db_session().expire_all()
    restarted = BrowserSession.get_for_id(session_id)
    assert restarted.payload.decrypt() == payload

    deleted = client.delete(f"/api/browser-sessions/{session_id}")
    assert deleted.status_code == 204
    assert not BrowserSession.list_all()


def test_create_list_get_and_rename(client):
    created = client.post(
        "/api/browser-sessions",
        json={"name": "linkedin", "payloadFormat": "profile_dir", "site": "linkedin.com"},
    )
    assert created.status_code == 200
    session_id = created.json()["id"]
    assert created.json()["payloadFormat"] == BrowserSessionPayloadFormat.PROFILE_DIR

    renamed = client.patch(
        f"/api/browser-sessions/{session_id}",
        json={"name": "linkedin-sales"},
    )
    assert renamed.status_code == 200
    assert renamed.json()["name"] == "linkedin-sales"
    assert client.get(f"/api/browser-sessions/{session_id}").json()["name"] == "linkedin-sales"
    assert [row["name"] for row in client.get("/api/browser-sessions").json()] == ["linkedin-sales"]


def test_duplicate_create_conflicts(client):
    body = {"name": "x-main", "site": "x.com"}
    assert client.post("/api/browser-sessions", json=body).status_code == 200
    assert client.post("/api/browser-sessions", json=body).status_code == 409


@pytest.mark.parametrize("name", ["", "Has Caps", "starts_with_underscore", "ends-"])
def test_create_rejects_names_that_are_not_slugs(client, name):
    response = client.post(
        "/api/browser-sessions",
        json={"name": name, "payloadFormat": "storage_state", "site": "x.com"},
    )
    assert response.status_code == 422


def test_upload_rejects_payloads_above_the_cap(client, monkeypatch):
    created = client.post(
        "/api/browser-sessions",
        json={"name": "x-main", "payloadFormat": "storage_state", "site": "x.com"},
    ).json()
    monkeypatch.setattr(routes, "MAX_PAYLOAD_BYTES", 3)

    response = client.put(f"/api/browser-sessions/{created['id']}/state", content=b"four")

    assert response.status_code == 413
    assert "256 MB" in response.json()["detail"]
    browser_session = BrowserSession.get_for_id(created["id"])
    assert browser_session.status == BrowserSessionStatus.NEEDS_LOGIN


def test_upload_warns_at_the_product_threshold(client, monkeypatch, caplog):
    created = client.post(
        "/api/browser-sessions",
        json={"name": "x-main", "payloadFormat": "storage_state", "site": "x.com"},
    ).json()
    monkeypatch.setattr(routes, "PAYLOAD_WARNING_BYTES", 3)

    with caplog.at_level("WARNING"):
        response = client.put(f"/api/browser-sessions/{created['id']}/state", content=b"three")

    assert response.status_code == 200
    assert "received a 5-byte payload" in caplog.text


def test_bearer_pat_reads_sessions_but_cannot_mutate_them(tmp_path, druks_db):
    settings = make_settings(
        tmp_path,
        identity={"mode": "header", "header": "X-Edge-Email"},
    )
    browser_session = create_session()
    account = Account.get_or_create("op@example.com")
    _, token = PersonalAccessToken.create(account_id=account.id, name="agent")
    db_session().commit()
    headers = {"Authorization": f"Bearer {token}"}

    with TestClient(configure_app_for_test(settings=settings, authenticated=False)) as pat_client:
        assert pat_client.get("/api/browser-sessions", headers=headers).status_code == 200
        mutations = (
            pat_client.post(
                "/api/browser-sessions",
                json={"name": "blocked", "payloadFormat": "storage_state", "site": "x.com"},
                headers=headers,
            ),
            pat_client.put(
                f"/api/browser-sessions/{browser_session.id}/state",
                content=b"blocked",
                headers=headers,
            ),
            pat_client.patch(
                f"/api/browser-sessions/{browser_session.id}",
                json={"name": "blocked"},
                headers=headers,
            ),
            pat_client.delete(f"/api/browser-sessions/{browser_session.id}", headers=headers),
        )

    assert [response.status_code for response in mutations] == [401, 401, 401, 401]

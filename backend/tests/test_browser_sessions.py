import base64

import pytest
from druks.accounts.dependencies import current_account, current_session_account
from druks.accounts.models import Account, PersonalAccessToken
from druks.browser import routes
from druks.browser.enums import BrowserSessionPayloadFormat, BrowserSessionStatus
from druks.browser.models import StoredBrowserSession
from druks.browser.sessions import BrowserSession
from druks.database import db_session
from druks.secrets import utils as secret_utils
from druks.secrets.exceptions import SecretDecryptError
from druks.testing import configure_app_for_test, make_settings
from fastapi.testclient import TestClient
from sqlalchemy import text


@pytest.fixture
def x_me(browser_session_declarations):
    class XMe:
        name = "x_me"
        x = BrowserSession(site="x.com")
        docs = BrowserSession(site="docs.example")

    return XMe


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


class FakeLoginWindow:
    opened: list[str] = []

    @classmethod
    async def open(cls, session) -> None:
        cls.opened.append(session.name)


def test_declared_sessions_list_without_a_row_and_the_pane_read_writes_nothing(client, x_me):
    listed = client.get("/api/browser-sessions").json()

    assert [entry["name"] for entry in listed] == ["x_me.docs", "x_me.x"]
    entry = listed[1]
    assert entry["status"] == BrowserSessionStatus.NEEDS_LOGIN
    assert entry["isDeclared"] is True
    assert entry["payloadFormat"] is None
    assert entry["createdAt"] is None
    assert entry["site"] == "x.com"
    assert not StoredBrowserSession.list_all()


def test_leftover_rows_list_as_undeclared_and_refuse_the_login_window(client, x_me):
    StoredBrowserSession.get_or_create(
        name="gone_ext.old",
        payload_format=BrowserSessionPayloadFormat.PROFILE_DIR,
        site="gone.example",
    )

    listed = client.get("/api/browser-sessions").json()
    assert [(entry["name"], entry["isDeclared"]) for entry in listed] == [
        ("x_me.docs", True),
        ("x_me.x", True),
        ("gone_ext.old", False),
    ]

    assert client.post("/api/browser-sessions/gone_ext.old/login-window").status_code == 404
    assert client.delete("/api/browser-sessions/gone_ext.old").status_code == 204
    assert not StoredBrowserSession.list_all()


def test_opening_the_login_window_materializes_the_declared_row(client, x_me, monkeypatch):
    monkeypatch.setattr(routes, "LoginWindow", FakeLoginWindow)
    monkeypatch.setattr(FakeLoginWindow, "opened", [])

    opened = client.post("/api/browser-sessions/x_me.x/login-window")

    assert opened.status_code == 204
    assert FakeLoginWindow.opened == ["x_me.x"]
    row = StoredBrowserSession.get_for_name("x_me.x")
    assert row.status == BrowserSessionStatus.NEEDS_LOGIN.value
    assert row.site == "x.com"

    assert client.post("/api/browser-sessions/nobody.home/login-window").status_code == 404


def test_import_materializes_the_row_survives_restart_and_delete_removes_it(
    client, x_me, tmp_path, monkeypatch
):
    payload = b'{"cookies":[{"name":"auth_token","value":"secret"}],"origins":[]}'
    uploaded = client.put(
        "/api/browser-sessions/x_me.x/state?payloadFormat=storage_state",
        content=payload,
        headers={"Content-Type": "application/octet-stream"},
    )
    assert uploaded.status_code == 204
    listed = {entry["name"]: entry for entry in client.get("/api/browser-sessions").json()}
    assert listed["x_me.x"]["status"] == BrowserSessionStatus.READY
    assert listed["x_me.x"]["payloadFormat"] == BrowserSessionPayloadFormat.STORAGE_STATE
    assert listed["x_me.x"]["lastRefreshedAt"]

    row = StoredBrowserSession.get_for_name("x_me.x")
    stored = (
        db_session()
        .execute(
            text("SELECT payload FROM browser_sessions WHERE id = :id"),
            {"id": row.id},
        )
        .scalar_one()
    )
    assert payload not in bytes(stored)
    with pytest.raises(SecretDecryptError):
        secret_utils.decrypt(bytes(stored), "another_table.payload")
    assert row.payload.decrypt() == payload

    wrong_key = base64.b64encode(b"1" * 32).decode()
    wrong_settings = make_settings(tmp_path / "wrong", secrets={"secrets_key": wrong_key})
    with monkeypatch.context() as patch:
        patch.setattr(secret_utils, "load_settings", lambda: wrong_settings)
        with pytest.raises(SecretDecryptError):
            row.payload.decrypt()

    db_session().expire_all()
    restarted = StoredBrowserSession.get_for_name("x_me.x")
    assert restarted.payload.decrypt() == payload

    undeclared = client.put(
        "/api/browser-sessions/nobody.home/state?payloadFormat=storage_state", content=b"x"
    )
    assert undeclared.status_code == 404

    deleted = client.delete("/api/browser-sessions/x_me.x")
    assert deleted.status_code == 204
    assert not StoredBrowserSession.list_all()


def test_upload_rejects_payloads_above_the_cap(client, x_me, monkeypatch):
    monkeypatch.setattr(routes, "MAX_PAYLOAD_BYTES", 3)

    response = client.put(
        "/api/browser-sessions/x_me.x/state?payloadFormat=storage_state", content=b"four"
    )

    assert response.status_code == 413
    assert "256 MB" in response.json()["detail"]
    listed = {entry["name"]: entry for entry in client.get("/api/browser-sessions").json()}
    assert listed["x_me.x"]["status"] == BrowserSessionStatus.NEEDS_LOGIN


def test_upload_warns_at_the_product_threshold(client, x_me, monkeypatch, caplog):
    monkeypatch.setattr(routes, "PAYLOAD_WARNING_BYTES", 3)

    with caplog.at_level("WARNING"):
        response = client.put(
            "/api/browser-sessions/x_me.x/state?payloadFormat=storage_state", content=b"three"
        )

    assert response.status_code == 204
    assert "received a 5-byte payload" in caplog.text


def test_bearer_pat_reads_sessions_but_cannot_mutate_them(tmp_path, druks_db, x_me):
    settings = make_settings(
        tmp_path,
        identity={"mode": "header", "header": "X-Edge-Email"},
    )
    account = Account.get_or_create("op@example.com")
    _, token = PersonalAccessToken.create(account_id=account.id, name="agent")
    db_session().commit()
    headers = {"Authorization": f"Bearer {token}"}

    with TestClient(configure_app_for_test(settings=settings, authenticated=False)) as pat_client:
        assert pat_client.get("/api/browser-sessions", headers=headers).status_code == 200
        mutations = (
            pat_client.put(
                "/api/browser-sessions/x_me.x/state?payloadFormat=storage_state",
                content=b"blocked",
                headers=headers,
            ),
            pat_client.post("/api/browser-sessions/x_me.x/login-window", headers=headers),
            pat_client.delete("/api/browser-sessions/x_me.x", headers=headers),
        )

    assert [response.status_code for response in mutations] == [401, 401, 401]

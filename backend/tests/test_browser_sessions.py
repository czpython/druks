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
def night_watch(browser_session_declarations):
    class NightWatch:
        name = "night_watch"
        acme = BrowserSession(site="acme.example")
        docs = BrowserSession(site="docs.example")

    return NightWatch


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


async def test_declared_sessions_list_without_a_row_and_the_pane_read_writes_nothing(
    client, night_watch
):
    listed = client.get("/api/browser-sessions").json()

    assert [entry["name"] for entry in listed] == ["night_watch.acme", "night_watch.docs"]
    entry = listed[0]
    assert entry["status"] == BrowserSessionStatus.NEEDS_LOGIN
    assert entry["isDeclared"] is True
    assert entry["payloadFormat"] is None
    assert entry["createdAt"] is None
    assert entry["site"] == "acme.example"
    assert not await StoredBrowserSession.list_all()


async def test_leftover_rows_list_as_undeclared_and_refuse_the_login_window(client, night_watch):
    await StoredBrowserSession.get_or_create(
        name="gone_ext.old",
        payload_format=BrowserSessionPayloadFormat.PROFILE_DIR,
        site="gone.example",
    )

    listed = client.get("/api/browser-sessions").json()
    assert [(entry["name"], entry["isDeclared"]) for entry in listed] == [
        ("night_watch.acme", True),
        ("night_watch.docs", True),
        ("gone_ext.old", False),
    ]

    assert client.post("/api/browser-sessions/gone_ext.old/login-window").status_code == 404
    assert client.delete("/api/browser-sessions/gone_ext.old").status_code == 204
    assert not await StoredBrowserSession.list_all()


async def test_anonymous_sessions_list_as_anonymous_and_refuse_login_and_state(
    client, browser_session_declarations
):
    class Critic:
        name = "critic"
        target = BrowserSession(site="druks.local", anonymous=True)

    listed = {entry["name"]: entry for entry in client.get("/api/browser-sessions").json()}
    assert listed["critic.target"]["status"] == BrowserSessionStatus.ANONYMOUS

    refused = client.post("/api/browser-sessions/critic.target/login-window")
    assert refused.status_code == 409
    assert "anonymous" in refused.json()["detail"]

    uploaded = client.put(
        "/api/browser-sessions/critic.target/state?payloadFormat=storage_state", content=b"x"
    )
    assert uploaded.status_code == 409
    assert not await StoredBrowserSession.list_all()


async def test_opening_the_login_window_materializes_the_declared_row(
    client, night_watch, monkeypatch
):
    monkeypatch.setattr(routes, "LoginWindow", FakeLoginWindow)
    monkeypatch.setattr(FakeLoginWindow, "opened", [])

    opened = client.post("/api/browser-sessions/night_watch.acme/login-window")

    assert opened.status_code == 204
    assert FakeLoginWindow.opened == ["night_watch.acme"]
    row = await StoredBrowserSession.get_for_name("night_watch.acme")
    assert row.status == BrowserSessionStatus.NEEDS_LOGIN.value
    assert row.site == "acme.example"

    assert client.post("/api/browser-sessions/nobody.home/login-window").status_code == 404


async def test_import_materializes_the_row_survives_restart_and_delete_removes_it(
    client, night_watch, tmp_path, monkeypatch
):
    payload = b'{"cookies":[{"name":"auth_token","value":"secret"}],"origins":[]}'
    uploaded = client.put(
        "/api/browser-sessions/night_watch.acme/state?payloadFormat=storage_state",
        content=payload,
        headers={"Content-Type": "application/octet-stream"},
    )
    assert uploaded.status_code == 204
    listed = {entry["name"]: entry for entry in client.get("/api/browser-sessions").json()}
    assert listed["night_watch.acme"]["status"] == BrowserSessionStatus.READY
    assert listed["night_watch.acme"]["payloadFormat"] == BrowserSessionPayloadFormat.STORAGE_STATE
    assert listed["night_watch.acme"]["lastRefreshedAt"]

    row = await StoredBrowserSession.get_for_name("night_watch.acme")
    stored = (
        await db_session().execute(
            text("SELECT payload FROM browser_sessions WHERE id = :id"),
            {"id": row.id},
        )
    ).scalar_one()
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

    db_session().expunge_all()
    restarted = await StoredBrowserSession.get_for_name("night_watch.acme")
    assert restarted.payload.decrypt() == payload

    undeclared = client.put(
        "/api/browser-sessions/nobody.home/state?payloadFormat=storage_state", content=b"x"
    )
    assert undeclared.status_code == 404

    deleted = client.delete("/api/browser-sessions/night_watch.acme")
    assert deleted.status_code == 204
    assert not await StoredBrowserSession.list_all()


def test_upload_rejects_payloads_above_the_cap(client, night_watch, monkeypatch):
    monkeypatch.setattr(routes, "MAX_PAYLOAD_BYTES", 3)

    response = client.put(
        "/api/browser-sessions/night_watch.acme/state?payloadFormat=storage_state", content=b"four"
    )

    assert response.status_code == 413
    assert "256 MB" in response.json()["detail"]
    listed = {entry["name"]: entry for entry in client.get("/api/browser-sessions").json()}
    assert listed["night_watch.acme"]["status"] == BrowserSessionStatus.NEEDS_LOGIN


def test_upload_warns_at_the_product_threshold(client, night_watch, monkeypatch, caplog):
    monkeypatch.setattr(routes, "PAYLOAD_WARNING_BYTES", 3)

    with caplog.at_level("WARNING"):
        response = client.put(
            "/api/browser-sessions/night_watch.acme/state?payloadFormat=storage_state",
            content=b"three",
        )

    assert response.status_code == 204
    assert "received a 5-byte payload" in caplog.text


async def test_bearer_pat_reads_sessions_but_cannot_mutate_them(tmp_path, druks_db, night_watch):
    settings = make_settings(
        tmp_path,
        identity={"mode": "header", "header": "X-Edge-Email"},
    )
    account = await Account.get_or_create("op@example.com")
    _, token = await PersonalAccessToken.create(account_id=account.id, name="agent")
    await db_session().commit()
    headers = {"Authorization": f"Bearer {token}"}

    with TestClient(configure_app_for_test(settings=settings, authenticated=False)) as pat_client:
        assert pat_client.get("/api/browser-sessions", headers=headers).status_code == 200
        mutations = (
            pat_client.put(
                "/api/browser-sessions/night_watch.acme/state?payloadFormat=storage_state",
                content=b"blocked",
                headers=headers,
            ),
            pat_client.post("/api/browser-sessions/night_watch.acme/login-window", headers=headers),
            pat_client.delete("/api/browser-sessions/night_watch.acme", headers=headers),
        )

    assert [response.status_code for response in mutations] == [401, 401, 401]

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from druks.accounts.dependencies import require_operator
from druks.browser import login
from druks.browser.enums import BrowserSessionPayloadFormat, BrowserSessionStatus
from druks.browser.exceptions import BrowserExportError, BrowserLoginWindowGoneError
from druks.browser.login import LoginWindow, is_same_origin
from druks.browser.models import StoredBrowserSession
from druks.database import db_session
from druks.sandbox.datastructures import ExecResult
from druks.secrets import utils as secret_utils
from druks.testing import make_settings
from fastapi import HTTPException, WebSocket

STATE_META_PATH = "/work/session/state.meta.json"
STATE_PATH = "/work/session/state.json"
PROFILE_PATH = "/work/session/state.tar.gz"
OUTPUT_STATE_PATH = "/work/session/out/state.tar.gz"


class FakeSandbox:
    def __init__(self, sandbox_id: str) -> None:
        self.id = sandbox_id
        self.files: dict[str, bytes] = {}
        self.export_exit_code = 0
        self.export_payload = b"fresh-profile"

    async def exec(self, command: list[str], *, timeout: float = 30.0) -> ExecResult:
        del timeout
        if command[:2] == ["sh", "-c"] and "session-launch --headed" in command[2]:
            self.files["/work/session/.runtime/ready.json"] = b"{}\n"
        if command == ["session-export"]:
            if self.export_exit_code:
                return ExecResult(exit_code=self.export_exit_code, stdout="", stderr="failed")
            self.files[OUTPUT_STATE_PATH] = self.export_payload
        return ExecResult(exit_code=0, stdout="", stderr="")

    async def upload_file(self, *, local, remote: str, mode: int = 0o600) -> None:
        del mode
        self.files[remote] = local.read_bytes()

    async def download(self, *, remote: str, local) -> None:
        local.write_bytes(self.files[remote])

    async def aclose(self) -> None:
        pass


class FakeSandboxClient:
    def __init__(self) -> None:
        self.browsers: list[FakeSandbox] = []
        self.provisions: list[dict[str, str]] = []
        self.released: list[str] = []

    async def provision(self, *, image_override: str, provider: str) -> FakeSandbox:
        self.provisions.append({"image_override": image_override, "provider": provider})
        browser = FakeSandbox(f"browser-{len(self.browsers) + 1}")
        self.browsers.append(browser)
        return browser

    @asynccontextmanager
    async def attach(self, *, host_id: str):
        yield next(browser for browser in self.browsers if browser.id == host_id)

    async def release(self, *, host_id: str) -> None:
        self.released.append(host_id)


@pytest.fixture
def window_runtime(tmp_path, monkeypatch):
    settings = make_settings(tmp_path)
    client = FakeSandboxClient()
    monkeypatch.setattr(login, "sandbox_client", client)
    monkeypatch.setattr(login, "load_settings", lambda: settings)
    monkeypatch.setattr(secret_utils, "load_settings", lambda: settings)
    return client


def create_session(name: str = "x-main") -> StoredBrowserSession:
    return StoredBrowserSession.create(
        name=name,
        payload_format=BrowserSessionPayloadFormat.STORAGE_STATE,
        site="x.com",
    )


async def test_open_seeds_a_blank_profile_and_records_the_container(window_runtime):
    client = window_runtime
    session = create_session()

    await LoginWindow.open(session)

    browser = client.browsers[0]
    assert json.loads(browser.files[STATE_META_PATH]) == {
        "format": BrowserSessionPayloadFormat.PROFILE_DIR,
        "version": 0,
    }
    assert STATE_PATH not in browser.files
    assert PROFILE_PATH not in browser.files
    assert (await LoginWindow.get_for_session(session.id)).host_id == browser.id
    assert client.provisions == [
        {"image_override": "ghcr.io/czpython/druks-browser:latest", "provider": "docker"}
    ]


async def test_reopening_disposes_the_previous_window(window_runtime):
    client = window_runtime
    session = create_session()
    await LoginWindow.open(session)

    await LoginWindow.open(session)

    assert client.released == ["browser-1"]
    assert (await LoginWindow.get_for_session(session.id)).host_id == "browser-2"


async def test_storage_state_reconnect_saves_a_profile(window_runtime):
    client = window_runtime
    session = create_session()
    session.store_payload(b'{"cookies": [{"name": "login"}], "origins": []}')
    session.mark_stale()

    await LoginWindow.open(session)
    browser = client.browsers[0]
    assert browser.files[STATE_PATH]
    assert json.loads(browser.files[STATE_META_PATH]) == {
        "format": BrowserSessionPayloadFormat.STORAGE_STATE,
        "version": 1,
    }

    saved = await (await LoginWindow.get_for_session(session.id)).save()

    assert saved.payload_format == BrowserSessionPayloadFormat.PROFILE_DIR
    db_session().expire_all()
    stored = StoredBrowserSession.get_for_id(session.id)
    assert stored.status == BrowserSessionStatus.READY.value
    assert stored.payload.decrypt() == b"fresh-profile"
    assert client.released == [browser.id]
    with pytest.raises(BrowserLoginWindowGoneError):
        await LoginWindow.get_for_session(session.id)


async def test_failed_export_closes_the_window(window_runtime):
    client = window_runtime
    session = create_session()
    await LoginWindow.open(session)
    client.browsers[0].export_exit_code = 1

    with pytest.raises(BrowserExportError):
        await (await LoginWindow.get_for_session(session.id)).save()

    assert client.released == [client.browsers[0].id]
    with pytest.raises(BrowserLoginWindowGoneError):
        await LoginWindow.get_for_session(session.id)


async def test_cancel_then_cancel_again_reports_the_window_gone(window_runtime):
    client = window_runtime
    session = create_session()
    await LoginWindow.open(session)

    await (await LoginWindow.get_for_session(session.id)).cancel()

    assert client.released == [client.browsers[0].id]
    with pytest.raises(BrowserLoginWindowGoneError):
        await LoginWindow.get_for_session(session.id)


async def test_get_for_session_reports_a_missing_window():
    with pytest.raises(BrowserLoginWindowGoneError):
        await LoginWindow.get_for_session("nope")


def _websocket(headers: list[tuple[bytes, bytes]], *, app=None) -> WebSocket:
    async def receive():
        return {"type": "websocket.disconnect"}

    async def send(message):
        del message

    return WebSocket(
        {
            "type": "websocket",
            "asgi": {"version": "3.0"},
            "scheme": "ws",
            "path": "/api/browser-sessions/s/login-window/ws",
            "raw_path": b"/api/browser-sessions/s/login-window/ws",
            "query_string": b"",
            "headers": headers,
            "client": ("testclient", 50000),
            "server": ("druks.test", 80),
            "subprotocols": [],
            "app": app,
        },
        receive,
        send,
    )


async def test_websocket_identity_resolves_and_cross_origin_is_refused(tmp_path):
    settings = make_settings(tmp_path, identity={"mode": "header", "header": "X-Edge-Email"})
    connection = _websocket(
        [
            (b"host", b"druks.test"),
            (b"origin", b"http://attacker.test"),
            (b"x-edge-email", b"operator@example.com"),
        ],
        app=SimpleNamespace(state=SimpleNamespace(settings=settings)),
    )

    account = await require_operator(connection)
    assert account.username == "operator@example.com"
    assert not is_same_origin(connection)


def test_tls_origin_matches_on_host_alone():
    assert is_same_origin(
        _websocket([(b"host", b"druks.test"), (b"origin", b"https://druks.test")])
    )


async def test_websocket_bearer_is_refused():
    connection = _websocket(
        [
            (b"host", b"druks.test"),
            (b"origin", b"http://druks.test"),
            (b"authorization", b"Bearer secret"),
        ]
    )
    with pytest.raises(HTTPException, match="never a bearer token"):
        await require_operator(connection)

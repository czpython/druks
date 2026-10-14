import asyncio
import json
import re
import shlex
import sys
import types
from contextlib import asynccontextmanager
from functools import partial
from unittest.mock import AsyncMock

import httpx
import pytest
from druks.browser import sessions as sessions_module
from druks.browser.enums import BrowserSessionPayloadFormat, BrowserSessionStatus
from druks.browser.exceptions import (
    BrowserClientMissingError,
    BrowserCommandError,
    BrowserLaunchError,
    BrowserSessionNotReadyError,
    BrowserSessionSignedOutError,
    BrowserSessionWriterLockedError,
)
from druks.browser.models import StoredBrowserSession
from druks.browser.sessions import BrowserSession
from druks.browser.subscribers import signed_out_session_goes_stale
from druks.db import db_session
from druks.redis import get_client
from druks.sandbox.datastructures import ExecResult
from druks.testing import make_settings


@pytest.fixture
def notebook(browser_session_declarations):
    class Notebook:
        name = "notebook"
        archive = BrowserSession(site="archive.example", persist=True)
        journal = BrowserSession(site="journal.example")
        weather = BrowserSession(site="weather.example", anonymous=True)
        catalog = BrowserSession(site="catalog.example", read_only=True)

    return Notebook


class FakeListener:
    is_closed = False

    def get_port(self):
        return 43987

    def close(self):
        self.is_closed = True


class FakeBrowser:
    def __init__(self):
        self.files = {}
        self.commands = []
        self.launch_exit = 0
        self.export_exit = 0
        self.listener = None

    async def exec(self, command, *, timeout=None):
        self.commands.append(command)
        if command[0] == "/work/session/session-export":
            self.files["/work/session/out/state.tar.gz"] = b"exported-profile"
            return ExecResult(exit_code=self.export_exit, stdout="", stderr="export stderr")
        return ExecResult(exit_code=self.launch_exit, stdout="", stderr="launch stderr")

    async def upload_file(self, *, local, remote, mode=0o600):
        self.files[remote] = local.read_bytes()

    async def download(self, *, remote, local):
        local.write_bytes(self.files[remote])

    async def forward_local_port(self, remote_port):
        self.forwarded_port = remote_port
        self.listener = FakeListener()
        return self.listener


@pytest.fixture
def borrow(druks_db, tmp_path, monkeypatch):
    settings = make_settings(tmp_path)
    monkeypatch.setattr(sessions_module, "load_settings", lambda: settings)
    browser = FakeBrowser()

    @asynccontextmanager
    async def ephemeral(*, provider, template):
        browser.provider = provider
        browser.template = template
        try:
            yield browser
        finally:
            browser.is_released = True

    monkeypatch.setattr(
        sessions_module, "sandbox_client", type("Client", (), {"ephemeral": ephemeral})
    )
    monkeypatch.setattr(
        sessions_module, "get_template_id", AsyncMock(return_value="browser-template")
    )
    return browser


async def stored_session(
    declaration: BrowserSession, payload: bytes = b"stored-state"
) -> StoredBrowserSession:
    row = await StoredBrowserSession.get_or_create(
        db_session(),
        name=declaration.name,
        payload_format=BrowserSessionPayloadFormat.STORAGE_STATE,
        site=declaration.site,
    )
    await row.store_payload(payload)
    return row


async def writer_locks() -> list[bytes]:
    return await get_client().keys("browser_session:*")


def test_declaration_carries_the_app_namespace(notebook):
    assert notebook.archive.name == "notebook.archive"
    assert notebook.journal.name == "notebook.journal"


async def test_status_reads_the_row_and_writes_nothing(druks_db, notebook):
    """A page reads where the login stands: the declaration's own status while
    no row exists, then the row's."""
    assert await notebook.journal.get_status() == BrowserSessionStatus.NEEDS_LOGIN
    assert await notebook.weather.get_status() == BrowserSessionStatus.ANONYMOUS
    assert not await StoredBrowserSession.list_all(db_session())

    row = await StoredBrowserSession.get_or_create(
        db_session(),
        name=notebook.journal.name,
        payload_format=BrowserSessionPayloadFormat.PROFILE_DIR,
        site=notebook.journal.site,
    )
    await row.mark_stale()

    assert await notebook.journal.get_status() == BrowserSessionStatus.STALE


async def test_get_status_does_not_load_the_payload(druks_db, notebook, monkeypatch):
    """A page asks where the login stands. The profile stays on the row."""
    await stored_session(notebook.journal, payload=b"stored-profile")
    statements: list[str] = []
    original = type(db_session()).scalar

    async def capture(self, statement, *args, **kwargs):
        statements.append(str(statement.compile(compile_kwargs={"literal_binds": False})))
        return await original(self, statement, *args, **kwargs)

    monkeypatch.setattr(type(db_session()), "scalar", capture)

    assert await notebook.journal.get_status() == BrowserSessionStatus.READY
    assert statements
    assert all("payload" not in sql.lower() for sql in statements)


async def test_borrow_yields_a_tunneled_cdp_url(borrow, notebook):
    browser = borrow
    await stored_session(notebook.journal)

    async with notebook.journal.cdp() as cdp_url:
        assert cdp_url == "http://127.0.0.1:43987"

    assert browser.forwarded_port == 9222
    assert browser.provider == "docker"
    assert browser.template == "browser-template"
    assert browser.files["/work/session/state.json"] == b"stored-state"
    assert json.loads(browser.files["/work/session/state.meta.json"]) == {
        "format": "storage_state",
        "version": 1,
    }
    launch_script = browser.commands[0][2]
    assert "session-launch --headed >/work/session/launch.log" in launch_script
    assert "DRUKS_BROWSER_CLIENT" not in launch_script
    assert "DRUKS_BROWSER_READ_ONLY" not in launch_script
    assert not await writer_locks()
    assert (
        await StoredBrowserSession.get_for_name(db_session(), notebook.journal.name)
    ).last_used_at


async def test_read_only_borrow_launches_behind_the_get_fence(borrow, notebook):
    browser = borrow
    await stored_session(notebook.catalog)

    async with notebook.catalog.cdp():
        pass

    assert "DRUKS_BROWSER_READ_ONLY=1" in browser.commands[0][2]


async def test_headless_declaration_launches_headless(borrow):
    browser = borrow
    quiet = BrowserSession(site="journal.example")
    quiet.headless = True
    quiet.name = "notebook.quiet"
    await stored_session(quiet)

    async with quiet.cdp():
        pass

    assert "session-launch --headless >/work/session/launch.log" in browser.commands[0][2]


async def test_borrow_launch_routes_through_the_proxy_when_the_scope_is_all(
    borrow, notebook, tmp_path, monkeypatch
):
    browser = borrow
    proxy = "http://172.17.0.1:8888"
    settings = make_settings(
        tmp_path,
        browser={"proxy": proxy, "proxy_scope": "all", "timezone": "Europe/Madrid"},
    )
    monkeypatch.setattr(sessions_module, "load_settings", lambda: settings)
    await stored_session(notebook.journal)

    async with notebook.journal.cdp():
        pass

    command = browser.commands[0][2]
    assert f"DRUKS_BROWSER_PROXY={shlex.quote(proxy)}" in command
    assert "TZ=Europe/Madrid" in command


async def test_borrow_launch_keeps_the_box_ip_under_the_login_scope(
    borrow, notebook, tmp_path, monkeypatch
):
    browser = borrow
    settings = make_settings(
        tmp_path, browser={"proxy": "http://172.17.0.1:8888", "timezone": "Europe/Madrid"}
    )
    monkeypatch.setattr(sessions_module, "load_settings", lambda: settings)
    await stored_session(notebook.journal)

    async with notebook.journal.cdp():
        pass

    command = browser.commands[0][2]
    assert "DRUKS_BROWSER_PROXY" not in command
    assert "TZ=" not in command


async def test_persisting_borrow_locks_exports_and_stores(borrow, notebook):
    browser = borrow
    row = await stored_session(notebook.archive)

    async with notebook.archive.cdp():
        assert await writer_locks()

    assert not await writer_locks()
    assert browser.commands[-1] == ["/work/session/session-export"]
    db_session().expunge_all()
    stored = await StoredBrowserSession.get_for_name(db_session(), notebook.archive.name)
    assert stored.payload.decrypt() == b"exported-profile"
    assert stored.payload_format == BrowserSessionPayloadFormat.PROFILE_DIR.value
    assert stored.id == row.id


async def test_persisting_borrow_refuses_a_second_writer(borrow, notebook):
    browser = borrow
    await stored_session(notebook.archive)
    row = await StoredBrowserSession.get_for_name(db_session(), notebook.archive.name)
    await get_client().set(f"browser_session:{row.id}", "other")

    with pytest.raises(BrowserSessionWriterLockedError):
        async with notebook.archive.cdp():
            pass

    assert browser.commands == []


async def test_first_borrow_writes_the_declared_session_and_asks_for_a_login(
    borrow, notebook, druks_db
):
    """The first borrow materializes the row and refuses to open a browser:
    the session is declared, but nobody has signed into it yet."""
    assert not await StoredBrowserSession.get_for_name(db_session(), notebook.journal.name)

    with pytest.raises(BrowserSessionNotReadyError):
        async with notebook.journal.cdp():
            pass

    row = await StoredBrowserSession.get_for_name(db_session(), notebook.journal.name)
    assert row.status == BrowserSessionStatus.NEEDS_LOGIN.value
    assert row.site == notebook.journal.site

    with pytest.raises(BrowserSessionNotReadyError):
        async with notebook.journal.cdp():
            pass

    assert await StoredBrowserSession.list_all(db_session()) == [row]


async def test_launch_failure_raises_and_releases_the_lock(borrow, notebook):
    browser = borrow
    await stored_session(notebook.archive)
    browser.launch_exit = 1

    with pytest.raises(BrowserLaunchError, match="launch stderr"):
        async with notebook.archive.cdp():
            pass

    assert not await writer_locks()


async def test_signed_out_borrow_stamps_the_session_and_stores_nothing(borrow, notebook):
    """The app raises through the borrow when the site bounced the login:
    the door stamps which session bounced, and the dead state is never stored."""
    browser = borrow
    await stored_session(notebook.archive, payload=b"live-state")

    with pytest.raises(BrowserSessionSignedOutError) as caught:
        async with notebook.archive.cdp():
            raise BrowserSessionSignedOutError("the site bounced the login")

    assert caught.value.session_name == "notebook.archive"
    db_session().expunge_all()
    assert (
        await StoredBrowserSession.get_for_name(db_session(), notebook.archive.name)
    ).payload.decrypt() == b"live-state"
    assert ["/work/session/session-export"] not in browser.commands
    assert not await writer_locks()  # the writer lock released on the way out


async def test_anonymous_borrow_needs_no_login(borrow, notebook):
    """An anonymous borrow works with zero operator setup: the browser opens
    on a blank profile, the row records the use, and nothing is stored."""
    browser = borrow

    async with notebook.weather.cdp() as cdp_url:
        assert cdp_url == "http://127.0.0.1:43987"

    assert sorted(browser.files) == [
        "/work/session/session-export",
        "/work/session/session-launch",
        "/work/session/state.meta.json",
    ]
    assert json.loads(browser.files["/work/session/state.meta.json"]) == {
        "format": "profile_dir",
        "version": 0,
    }
    assert ["/work/session/session-export"] not in browser.commands
    assert not await writer_locks()  # no writer lock: nothing to serialize
    row = await StoredBrowserSession.get_for_name(db_session(), notebook.weather.name)
    assert row.status == BrowserSessionStatus.ANONYMOUS.value
    assert row.last_used_at
    assert not row.payload


def test_anonymous_with_persist_fails_at_class_definition():
    with pytest.raises(ValueError, match="anonymous=True, persist=True"):
        BrowserSession(site="archive.example", anonymous=True, persist=True)


async def test_signed_out_in_an_anonymous_borrow_keeps_the_row_anonymous(borrow, notebook):
    """The run still fails under the signed-out reason, but there is no login
    to go stale: the subscriber leaves the row anonymous, never wanting a login."""
    with pytest.raises(BrowserSessionSignedOutError) as caught:
        async with notebook.weather.cdp():
            raise BrowserSessionSignedOutError("the target bounced the borrow")

    assert caught.value.session_name == "notebook.weather"
    await signed_out_session_goes_stale(session_name="notebook.weather")
    row = await StoredBrowserSession.get_for_name(db_session(), "notebook.weather")
    assert row.status == BrowserSessionStatus.ANONYMOUS.value


async def test_playwright_yields_the_logged_in_context(borrow, notebook, monkeypatch):

    await stored_session(notebook.journal)
    seen = {}
    logged_in_context = object()

    class Connection:
        contexts = [logged_in_context]

        async def close(self):
            seen["closed"] = True

    class Chromium:
        async def connect_over_cdp(self, url):
            seen["url"] = url
            return Connection()

    @asynccontextmanager
    async def fake_playwright():
        yield types.SimpleNamespace(chromium=Chromium())

    playwright_module = types.ModuleType("playwright.async_api")
    playwright_module.async_playwright = fake_playwright
    monkeypatch.setitem(sys.modules, "playwright", types.ModuleType("playwright"))
    monkeypatch.setitem(sys.modules, "playwright.async_api", playwright_module)

    async with notebook.journal.playwright() as context:
        assert context is logged_in_context

    assert seen == {"url": "http://127.0.0.1:43987", "closed": True}


async def test_playwright_waits_for_a_late_default_context(borrow, notebook, monkeypatch):

    await stored_session(notebook.journal)
    logged_in_context = object()

    class Connection:
        def __init__(self):
            self.looks = 0

        @property
        def contexts(self):
            self.looks += 1
            return [logged_in_context] if self.looks > 2 else []

        async def close(self):
            pass

    class Chromium:
        async def connect_over_cdp(self, url):
            del url
            return Connection()

    @asynccontextmanager
    async def fake_playwright():
        yield types.SimpleNamespace(chromium=Chromium())

    playwright_module = types.ModuleType("playwright.async_api")
    playwright_module.async_playwright = fake_playwright
    monkeypatch.setitem(sys.modules, "playwright", types.ModuleType("playwright"))
    monkeypatch.setitem(sys.modules, "playwright.async_api", playwright_module)

    async with notebook.journal.playwright() as context:
        assert context is logged_in_context


async def test_playwright_cdp_attach_timeout_is_a_launch_error(borrow, notebook, monkeypatch):

    await stored_session(notebook.journal)

    class Chromium:
        async def connect_over_cdp(self, url):
            del url
            await asyncio.sleep(60)

    @asynccontextmanager
    async def fake_playwright():
        yield types.SimpleNamespace(chromium=Chromium())

    playwright_module = types.ModuleType("playwright.async_api")
    playwright_module.async_playwright = fake_playwright
    monkeypatch.setitem(sys.modules, "playwright", types.ModuleType("playwright"))
    monkeypatch.setitem(sys.modules, "playwright.async_api", playwright_module)
    monkeypatch.setattr(sessions_module, "CDP_CONNECT_TIMEOUT_SECONDS", 0.01)

    with pytest.raises(BrowserLaunchError, match="timed out attaching"):
        async with notebook.journal.playwright():
            pass


async def test_playwright_without_the_dependency_names_the_fix(borrow, notebook, monkeypatch):

    await stored_session(notebook.journal)
    monkeypatch.setitem(sys.modules, "playwright", None)
    monkeypatch.setitem(sys.modules, "playwright.async_api", None)

    with pytest.raises(BrowserClientMissingError, match="Add playwright"):
        async with notebook.journal.playwright():
            pass


@pytest.fixture
def bridge_requests(monkeypatch):
    """The requests the PinchTab bridge receives through the forward."""
    requests = []
    replies = {
        "/navigate": {"tabId": "tab-1", "url": "https://journal.example/notes", "title": "Notes"},
        "/evaluate": {"result": ["first", "second"]},
        "/text": {"text": "Field notes"},
        "/snapshot": {"nodes": [{"ref": "e1", "role": "link", "name": "More"}]},
        "/action": {"success": True},
    }

    def answer(request):
        requests.append(request)
        return httpx.Response(200, json=replies[request.url.path])

    transport = httpx.MockTransport(answer)
    monkeypatch.setattr(httpx, "AsyncClient", partial(httpx.AsyncClient, transport=transport))
    return requests


async def test_pinchtab_drives_one_tab_through_the_bridge(borrow, bridge_requests, notebook):
    browser = borrow
    await stored_session(notebook.journal)

    async with notebook.journal.pinchtab() as pinchtab:
        await pinchtab.navigate("https://journal.example/notes")
        assert await pinchtab.evaluate("[...document.links].map((link) => link.href)") == [
            "first",
            "second",
        ]
        assert await pinchtab.text() == "Field notes"
        assert await pinchtab.snapshot() == {
            "nodes": [{"ref": "e1", "role": "link", "name": "More"}]
        }
        assert await pinchtab.action("click", ref="e1") == {"success": True}

    launch_script = browser.commands[0][2]
    assert "DRUKS_BROWSER_CLIENT=pinchtab" in launch_script
    assert "DRUKS_BROWSER_SITE=journal.example" in launch_script
    token = re.search(r"DRUKS_BROWSER_CLIENT_TOKEN=(\S+)", launch_script).group(1)
    assert {request.headers["authorization"] for request in bridge_requests} == {f"Bearer {token}"}
    assert browser.forwarded_port == 9867
    navigate, evaluate, text, snapshot, action = bridge_requests
    assert json.loads(navigate.content) == {"url": "https://journal.example/notes", "tabId": ""}
    assert json.loads(evaluate.content) == {
        "expression": "[...document.links].map((link) => link.href)",
        "awaitPromise": True,
        "tabId": "tab-1",
    }
    assert text.url.params["tabId"] == snapshot.url.params["tabId"] == "tab-1"
    assert json.loads(action.content) == {"kind": "click", "tabId": "tab-1", "ref": "e1"}
    assert browser.listener.is_closed
    assert browser.is_released


async def test_a_refused_command_raises_with_the_bridge_reason(borrow, notebook, monkeypatch):
    def refuse(request):
        return httpx.Response(500, json={"error": "evaluate: ReferenceError: x is not defined"})

    transport = httpx.MockTransport(refuse)
    monkeypatch.setattr(httpx, "AsyncClient", partial(httpx.AsyncClient, transport=transport))
    await stored_session(notebook.journal)

    async with notebook.journal.pinchtab() as pinchtab:
        with pytest.raises(BrowserCommandError, match="evaluate failed: .*ReferenceError"):
            await pinchtab.evaluate("x.length")


async def test_failed_attachment_leaves_no_borrow(borrow, notebook):
    browser = borrow
    browser.launch_exit = 1
    await stored_session(notebook.archive)

    with pytest.raises(BrowserLaunchError, match="launch stderr"):
        async with notebook.archive.pinchtab():
            pass

    assert not browser.listener
    assert browser.is_released
    assert not await writer_locks()


async def test_cancelled_pinchtab_borrow_releases_everything(borrow, bridge_requests, notebook):
    browser = borrow
    await stored_session(notebook.archive)

    with pytest.raises(asyncio.CancelledError):
        async with notebook.archive.pinchtab():
            assert await writer_locks()
            raise asyncio.CancelledError

    assert browser.listener.is_closed
    assert browser.is_released
    assert not await writer_locks()
    assert ["/work/session/session-export"] not in browser.commands

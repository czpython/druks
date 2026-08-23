import json
from contextlib import asynccontextmanager

import pytest
from druks.browser import locks
from druks.browser import sessions as sessions_module
from druks.browser.enums import BrowserSessionPayloadFormat, BrowserSessionStatus
from druks.browser.exceptions import (
    BrowserClientMissingError,
    BrowserLaunchError,
    BrowserSessionNotReadyError,
    BrowserSessionSignedOutError,
    BrowserSessionWriterLockedError,
)
from druks.browser.models import StoredBrowserSession
from druks.browser.sessions import BrowserSession
from druks.database import db_session
from druks.sandbox.datastructures import ExecResult
from druks.secrets import utils as secret_utils
from druks.testing import make_settings


@pytest.fixture
def night_watch(browser_session_declarations):
    class NightWatch:
        name = "night_watch"
        acme = BrowserSession(site="acme.example", persist=True)
        docs = BrowserSession(site="docs.example")

    return NightWatch


class FakeListener:
    def get_port(self):
        return 43987

    def close(self):
        pass


class FakeBrowser:
    def __init__(self):
        self.files = {}
        self.commands = []
        self.launch_exit = 0
        self.export_exit = 0

    async def exec(self, command, *, timeout=None):
        self.commands.append(command)
        if command[0] == "session-export":
            self.files["/work/session/out/state.tar.gz"] = b"exported-profile"
            return ExecResult(exit_code=self.export_exit, stdout="", stderr="export stderr")
        return ExecResult(exit_code=self.launch_exit, stdout="", stderr="launch stderr")

    async def upload_file(self, *, local, remote, mode=0o600):
        self.files[remote] = local.read_bytes()

    async def download(self, *, remote, local):
        local.write_bytes(self.files[remote])

    async def forward_local_port(self, remote_port):
        self.forwarded_port = remote_port
        return FakeListener()


class FakeRedis:
    def __init__(self):
        self.values = {}

    async def set(self, key, value, *, nx, ex):
        if nx and key in self.values:
            return False
        self.values[key] = value
        return True

    async def eval(self, script, _key_count, key, *arguments):
        owner_token = arguments[0]
        if self.values.get(key) != owner_token:
            return 0
        if "del" in script:
            del self.values[key]
        return 1


@pytest.fixture
def borrow(druks_db, tmp_path, monkeypatch):
    settings = make_settings(tmp_path)
    monkeypatch.setattr(secret_utils, "load_settings", lambda: settings)
    monkeypatch.setattr(sessions_module, "load_settings", lambda: settings)
    redis = FakeRedis()
    monkeypatch.setattr(locks, "get_client", lambda: redis)
    browser = FakeBrowser()

    @asynccontextmanager
    async def ephemeral(*, image_override, provider):
        browser.image = image_override
        browser.provider = provider
        yield browser

    monkeypatch.setattr(
        sessions_module, "sandbox_client", type("Client", (), {"ephemeral": ephemeral})
    )
    return browser, redis


def stored_session(
    declaration: BrowserSession, payload: bytes = b"stored-state"
) -> StoredBrowserSession:
    row = StoredBrowserSession.get_or_create(
        name=declaration.name,
        payload_format=BrowserSessionPayloadFormat.STORAGE_STATE,
        site=declaration.site,
    )
    row.store_payload(payload)
    return row


def test_declaration_carries_the_app_namespace(night_watch):
    assert night_watch.acme.name == "night_watch.acme"
    assert night_watch.docs.name == "night_watch.docs"


async def test_borrow_yields_a_tunneled_cdp_url(borrow, night_watch):
    browser, redis = borrow
    stored_session(night_watch.docs)

    async with night_watch.docs.cdp() as cdp_url:
        assert cdp_url == "http://127.0.0.1:43987"

    assert browser.forwarded_port == 9222
    assert browser.image == "ghcr.io/czpython/druks-browser:latest"
    assert browser.provider == "docker"
    assert browser.files["/work/session/state.json"] == b"stored-state"
    assert json.loads(browser.files["/work/session/state.meta.json"]) == {
        "format": "storage_state",
        "version": 1,
    }
    launch_script = browser.commands[0][2]
    assert "session-launch --headed" in launch_script
    assert not redis.values
    assert StoredBrowserSession.get_for_name(night_watch.docs.name).last_used_at


async def test_headless_declaration_launches_headless(borrow):
    browser, _ = borrow
    quiet = BrowserSession(site="docs.example")
    quiet.headless = True
    quiet.name = "night_watch.quiet"
    stored_session(quiet)

    async with quiet.cdp():
        pass

    assert "session-launch --headless" in browser.commands[0][2]


async def test_persisting_borrow_locks_exports_and_stores(borrow, night_watch):
    browser, redis = borrow
    row = stored_session(night_watch.acme)

    async with night_watch.acme.cdp():
        assert redis.values

    assert not redis.values
    assert browser.commands[-1] == ["session-export"]
    db_session().expire_all()
    stored = StoredBrowserSession.get_for_name(night_watch.acme.name)
    assert stored.payload.decrypt() == b"exported-profile"
    assert stored.payload_format == BrowserSessionPayloadFormat.PROFILE_DIR.value
    assert stored.id == row.id


async def test_persisting_borrow_refuses_a_second_writer(borrow, night_watch):
    browser, redis = borrow
    stored_session(night_watch.acme)
    redis.values[
        f"browser_session:{StoredBrowserSession.get_for_name(night_watch.acme.name).id}"
    ] = "other"

    with pytest.raises(BrowserSessionWriterLockedError):
        async with night_watch.acme.cdp():
            pass

    assert browser.commands == []


async def test_first_borrow_writes_the_declared_session_and_asks_for_a_login(
    borrow, night_watch, druks_db
):
    """The first borrow materializes the row and refuses to open a browser:
    the session is declared, but nobody has signed into it yet."""
    assert not StoredBrowserSession.get_for_name(night_watch.docs.name)

    with pytest.raises(BrowserSessionNotReadyError):
        async with night_watch.docs.cdp():
            pass

    row = StoredBrowserSession.get_for_name(night_watch.docs.name)
    assert row.status == BrowserSessionStatus.NEEDS_LOGIN.value
    assert row.site == night_watch.docs.site

    with pytest.raises(BrowserSessionNotReadyError):
        async with night_watch.docs.cdp():
            pass

    assert StoredBrowserSession.list_all() == [row]


async def test_launch_failure_raises_and_releases_the_lock(borrow, night_watch):
    browser, redis = borrow
    stored_session(night_watch.acme)
    browser.launch_exit = 1

    with pytest.raises(BrowserLaunchError, match="launch stderr"):
        async with night_watch.acme.cdp():
            pass

    assert not redis.values


async def test_signed_out_borrow_stamps_the_session_and_stores_nothing(borrow, night_watch):
    """The app raises through the borrow when the site bounced the login:
    the door stamps which session bounced, and the dead state is never stored."""
    browser, redis = borrow
    stored_session(night_watch.acme, payload=b"live-state")

    with pytest.raises(BrowserSessionSignedOutError) as caught:
        async with night_watch.acme.cdp():
            raise BrowserSessionSignedOutError("the site bounced the login")

    assert caught.value.session_name == "night_watch.acme"
    db_session().expire_all()
    assert (
        StoredBrowserSession.get_for_name(night_watch.acme.name).payload.decrypt() == b"live-state"
    )
    assert ["session-export"] not in browser.commands
    assert not redis.values  # the writer lock released on the way out


async def test_playwright_yields_the_logged_in_context(borrow, night_watch, monkeypatch):
    import sys
    import types
    from contextlib import asynccontextmanager as acm

    browser, _ = borrow
    stored_session(night_watch.docs)
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

    @acm
    async def fake_playwright():
        yield types.SimpleNamespace(chromium=Chromium())

    playwright_module = types.ModuleType("playwright.async_api")
    playwright_module.async_playwright = fake_playwright
    monkeypatch.setitem(sys.modules, "playwright", types.ModuleType("playwright"))
    monkeypatch.setitem(sys.modules, "playwright.async_api", playwright_module)

    async with night_watch.docs.playwright() as context:
        assert context is logged_in_context

    assert seen == {"url": "http://127.0.0.1:43987", "closed": True}


async def test_playwright_without_the_dependency_names_the_fix(borrow, night_watch, monkeypatch):
    import sys

    stored_session(night_watch.docs)
    monkeypatch.setitem(sys.modules, "playwright", None)
    monkeypatch.setitem(sys.modules, "playwright.async_api", None)

    with pytest.raises(BrowserClientMissingError, match="add playwright"):
        async with night_watch.docs.playwright():
            pass

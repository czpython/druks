import asyncio
import json
from contextlib import asynccontextmanager
from functools import partial

import httpx
import pytest
from druks.accounts.models import Account, PersonalAccessToken
from druks.browser import agent_browsers
from druks.browser.agent_browsers import AgentBrowser
from druks.browser.constants import BROWSER_TOOLS
from druks.browser.enums import BrowserClient
from druks.browser.sessions import BrowserSession
from druks.mcp.exceptions import MissingEndpointError
from druks.redis import get_client
from druks.secrets.models import VaultSecret
from druks.testing import configure_app_for_test, make_settings


class FakeListener:
    def get_port(self):
        return 43988

    def close(self):
        pass


class FakeBrowser:
    id = "browser-host"

    async def forward_local_port(self, remote_port):
        self.forwarded_port = remote_port
        return FakeListener()


@pytest.fixture
def atlas(druks_db, tmp_path, monkeypatch):
    """A session whose borrow records its client and token instead of launching."""
    settings = make_settings(tmp_path, urls={"webhook_host": "druks.example"})
    monkeypatch.setattr(agent_browsers, "load_settings", lambda: settings)
    session = BrowserSession(site="atlas.example", read_only=True)
    session.borrows = []

    @asynccontextmanager
    async def borrow(*, client, token):
        session.borrows.append({"client": client, "token": token, "is_open": True})
        try:
            yield FakeBrowser()
        finally:
            session.borrows[-1]["is_open"] = False

    session._borrow = borrow
    return session


@pytest.fixture
async def account(druks_db):
    return await Account.get_or_create(druks_db, "op@example.com")


@pytest.fixture
def bridge_requests(monkeypatch):
    """The requests that reach the bridge in the borrowed browser's box."""
    requests = []

    def answer(request):
        requests.append(request)
        if request.url.path == "/text":
            return httpx.Response(200, json={"text": "Atlas"})
        if request.url.path == "/action":
            return httpx.Response(400, json={"error": "ref e9 not found in snapshot"})
        return httpx.Response(200, json={"tabId": "tab-1", "url": "https://atlas.example/"})

    transport = httpx.MockTransport(answer)
    monkeypatch.setattr(httpx, "AsyncClient", partial(httpx.AsyncClient, transport=transport))

    @asynccontextmanager
    async def attach(*, host_id):
        assert host_id == "browser-host"
        yield FakeBrowser()

    monkeypatch.setattr(agent_browsers.sandbox_client, "attach", attach)
    return requests


@pytest.fixture
async def wire(tmp_path, druks_db):
    """Requests to Druks on the test's own loop, as a box sends them."""
    app = configure_app_for_test(settings=make_settings(tmp_path), authenticated=False)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://druks.example"
    ) as client:
        yield client


async def records() -> list[bytes]:
    return await get_client().keys("agent_browser:*")


async def key_of(druks_db, secret) -> PersonalAccessToken:
    row = await VaultSecret.reload(druks_db, secret.secret_id)
    return await PersonalAccessToken.get_for_prefix(druks_db, row.identity["token_prefix"])


async def bearer_of(druks_db, secret) -> dict[str, str]:
    row = await VaultSecret.reload(druks_db, secret.secret_id)
    return {"Authorization": row.secrets["value"]}


async def test_lend_gives_the_box_a_key_for_the_browser_tools(druks_db, atlas, account):
    async with AgentBrowser.lend(
        atlas, BrowserClient.PINCHTAB, account_id=account.id, call_id="call-1"
    ) as (server, secret):
        [borrow] = atlas.borrows
        assert borrow["client"] == BrowserClient.PINCHTAB
        assert (server.name, server.url, server.bearer_token_env_var) == (
            "browser",
            "https://druks.example/mcp",
            "MCP_BROWSER_TOKEN",
        )
        assert (secret.name, secret.host) == ("mcp_browser_token", "druks.example")
        key = await key_of(druks_db, secret)
        assert key.allowed_tools == list(BROWSER_TOOLS)
        lent = await AgentBrowser.get_for_key(key.token_prefix)
        assert (lent.host_id, lent.token, lent.tab_id) == ("browser-host", borrow["token"], "")

    await druks_db.refresh(key)
    assert key.status == "revoked"
    assert not await records()
    assert not borrow["is_open"]


async def test_a_cancelled_call_revokes_its_key_and_releases_the_browser(druks_db, atlas, account):
    with pytest.raises(asyncio.CancelledError):
        async with AgentBrowser.lend(
            atlas, BrowserClient.PINCHTAB, account_id=account.id, call_id="call-1"
        ) as (_, secret):
            key = await key_of(druks_db, secret)
            assert await records()
            raise asyncio.CancelledError

    await druks_db.refresh(key)
    assert key.status == "revoked"
    assert not await records()
    assert not atlas.borrows[0]["is_open"]


async def test_lend_needs_an_address_the_box_can_reach(atlas, account, tmp_path, monkeypatch):
    monkeypatch.setattr(agent_browsers, "load_settings", lambda: make_settings(tmp_path))

    with pytest.raises(MissingEndpointError, match="urls.webhook_host"):
        async with AgentBrowser.lend(
            atlas, BrowserClient.PINCHTAB, account_id=account.id, call_id="call-1"
        ):
            pass

    assert not atlas.borrows


async def test_the_tools_drive_the_bridge_on_the_call_s_tab(
    druks_db, atlas, account, bridge_requests, wire
):
    async with AgentBrowser.lend(
        atlas, BrowserClient.PINCHTAB, account_id=account.id, call_id="call-1"
    ) as (_, secret):
        headers = await bearer_of(druks_db, secret)
        opened = await wire.post(
            "/api/browser/navigate", headers=headers, json={"url": "https://atlas.example/"}
        )
        read = await wire.get("/api/browser/text", headers=headers)

    assert opened.status_code == 200
    assert opened.json() == {"tabId": "tab-1", "url": "https://atlas.example/"}
    assert read.json() == {"text": "Atlas"}
    [navigate, text] = bridge_requests
    assert navigate.headers["authorization"] == f"Bearer {atlas.borrows[0]['token']}"
    assert json.loads(navigate.content) == {"url": "https://atlas.example/", "tabId": ""}
    assert text.url.params["tabId"] == "tab-1"


async def test_a_refused_command_reaches_the_agent_with_the_browser_s_reason(
    druks_db, atlas, account, bridge_requests, wire
):
    async with AgentBrowser.lend(
        atlas, BrowserClient.PINCHTAB, account_id=account.id, call_id="call-1"
    ) as (_, secret):
        refused = await wire.post(
            "/api/browser/action",
            headers=await bearer_of(druks_db, secret),
            json={"kind": "click", "fields": {"ref": "e9"}},
        )

    assert refused.status_code == 400
    assert refused.json()["detail"] == "Browser action failed: ref e9 not found in snapshot"


async def test_a_late_tool_does_not_revive_an_ended_call_s_record(bridge_requests, druks_db):
    lent = AgentBrowser("key-1", "browser-host", "bridge-token")
    await lent.save()
    async with lent.pinchtab() as pinchtab:
        await get_client().delete(lent.record_key)
        await pinchtab.navigate("https://atlas.example/")

    assert not await records()


async def test_the_tools_answer_the_call_s_key_only(
    druks_db, atlas, account, bridge_requests, wire
):
    _, operator_key = await PersonalAccessToken.create(
        druks_db, account_id=account.id, name="operator"
    )
    async with AgentBrowser.lend(
        atlas, BrowserClient.PINCHTAB, account_id=account.id, call_id="call-1"
    ) as (_, secret):
        headers = await bearer_of(druks_db, secret)
        other_tool = await wire.get("/api/usage/summary", headers=headers)
        no_browser = await wire.get(
            "/api/browser/text", headers={"Authorization": f"Bearer {operator_key}"}
        )

    ended = await wire.get("/api/browser/text", headers=headers)

    assert (other_tool.status_code, no_browser.status_code, ended.status_code) == (403, 409, 401)
    assert not bridge_requests

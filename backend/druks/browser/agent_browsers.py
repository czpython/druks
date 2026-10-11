import json
import secrets
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from druks.accounts.models import Account, PersonalAccessToken
from druks.browser.constants import AGENT_BROWSER_KEY_PREFIX, BROWSER_TOOLS
from druks.browser.enums import BrowserClient
from druks.browser.exceptions import AgentBrowserGoneError
from druks.browser.pinchtab import PinchTab
from druks.database import session_scope
from druks.locks import lock
from druks.mcp.constants import BROWSER_SERVER_NAME
from druks.mcp.exceptions import MissingEndpointError
from druks.mcp.helpers import get_bearer_token_env_var
from druks.mcp.inbound import get_druks_account_token
from druks.redis import get_client
from druks.sandbox.client import sandbox_client
from druks.sandbox.constants import SANDBOX_HOST_LEASE_SECONDS
from druks.sandbox.datastructures import McpServer
from druks.sandbox.models import SecretRef
from druks.secrets.models import VaultSecret
from druks.settings import load_settings

if TYPE_CHECKING:
    from druks.browser.sessions import BrowserSession


class AgentBrowser:
    """A borrowed browser that one agent call drives through the browser tools."""

    def __init__(self, key_prefix: str, host_id: str, token: str, tab_id: str = "") -> None:
        self.key_prefix = key_prefix
        self.host_id = host_id
        self.token = token
        self.tab_id = tab_id

    @classmethod
    @asynccontextmanager
    async def lend(
        cls,
        session: "BrowserSession",
        client: BrowserClient,
        *,
        account_id: str | None,
        call_id: str,
    ):
        """Borrow ``session`` for the call under a key that allows the browser
        tools. Yields the server and the secret that the call's box gets. After
        the block, the key is revoked and the browser is released."""
        endpoint = load_settings().urls.webhook_base
        if not endpoint:
            raise MissingEndpointError(BROWSER_SERVER_NAME)
        variable = get_bearer_token_env_var(BROWSER_SERVER_NAME)
        server = McpServer(
            name=BROWSER_SERVER_NAME, url=f"{endpoint}/mcp", bearer_token_env_var=variable
        )
        # The key is committed on its own: the box's entry issues from the row
        # whatever the agent step's transaction does, and so does the revoke.
        async with session_scope() as db:
            account = await Account.get_for_run(db, account_id)
            key = await get_druks_account_token(
                db, account.id, BROWSER_TOOLS, name=f"{BROWSER_SERVER_NAME} {call_id}"
            )
            key_id, key_prefix = key.id, key.identity["token_prefix"]
        secret = SecretRef(
            name=variable.lower(), secret_id=key_id, host=urlsplit(server.url).hostname
        )
        token = secrets.token_urlsafe(32)
        try:
            async with session._borrow(client=client, token=token) as browser:
                lent = cls(key_prefix, browser.id, token)
                await lent.save()
                try:
                    yield server, secret
                finally:
                    await get_client().delete(lent.record_key)
        finally:
            async with session_scope() as db:
                await (await PersonalAccessToken.get_for_prefix(db, key_prefix)).revoke()
                await (await VaultSecret.reload(db, key_id)).revoke("agent call ended")

    @classmethod
    async def get_for_key(cls, key_prefix: str) -> "AgentBrowser":
        """The browser that the key drives. Raises once its call has ended."""
        if record := await get_client().get(f"{AGENT_BROWSER_KEY_PREFIX}{key_prefix}"):
            return cls(key_prefix, **json.loads(record))
        raise AgentBrowserGoneError

    @property
    def record_key(self) -> str:
        return f"{AGENT_BROWSER_KEY_PREFIX}{self.key_prefix}"

    @property
    def record(self) -> str:
        return json.dumps({"host_id": self.host_id, "token": self.token, "tab_id": self.tab_id})

    async def save(self) -> None:
        await get_client().set(self.record_key, self.record, ex=SANDBOX_HOST_LEASE_SECONDS)

    @asynccontextmanager
    async def pinchtab(self):
        """The bridge in the browser's box, on the call's tab. One tool at a
        time: a harness can call two at once, and both would open a tab."""
        async with (
            lock(f"lock:{self.record_key}"),
            sandbox_client.attach(host_id=self.host_id) as browser,
            PinchTab.connect(browser, self.token) as pinchtab,
        ):
            # The tab the last tool left, read under the lock.
            self.tab_id = (await self.get_for_key(self.key_prefix)).tab_id
            pinchtab.tab_id = self.tab_id
            yield pinchtab
            if pinchtab.tab_id != self.tab_id:
                self.tab_id = pinchtab.tab_id
                # Only while the call is open: a late tool must not revive the record.
                await get_client().set(self.record_key, self.record, xx=True, keepttl=True)

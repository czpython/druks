import asyncio
import json
import shlex
import tempfile
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

from druks.apps.registry import browser_sessions
from druks.browser.constants import (
    CDP_CONNECT_TIMEOUT_SECONDS,
    SESSION_EXPORT_TIMEOUT_SECONDS,
    SESSION_LAUNCH_TIMEOUT_SECONDS,
)
from druks.browser.enums import BrowserSessionPayloadFormat, BrowserSessionStatus
from druks.browser.exceptions import (
    BrowserClientMissingError,
    BrowserExportError,
    BrowserLaunchError,
    BrowserSessionNotReadyError,
    BrowserSessionSignedOutError,
    BrowserSessionWriterLockedError,
)
from druks.browser.models import StoredBrowserSession
from druks.db import db_session
from druks.exceptions import LockHeldError
from druks.locks import lock
from druks.sandbox.client import sandbox_client
from druks.sandbox.datastructures import Sandbox
from druks.sandbox.templates import get_template_id
from druks.settings import Settings, load_settings

SESSION_ROOT = "/work/session"
CDP_PORT = 9222


async def _playwright_context(connection, name: str):
    """The default context a borrow drives. Playwright-launched Chrome has it
    immediately; a raw debugger sometimes advertises it a beat late."""
    if connection.contexts:
        return connection.contexts[0]
    for _ in range(50):
        await asyncio.sleep(0.1)
        if connection.contexts:
            return connection.contexts[0]
    raise BrowserLaunchError(name, "Chrome opened without a browser context")


@dataclass
class BrowserSession:
    """A named browser login the app's runs borrow.

    Declared on the App class — ``acme = BrowserSession(site="acme.example")``
    — the attribute name and the app's name become the session's identity
    (``night_watch.acme``). The operator signs in once through the login window;
    a workflow then borrows the logged-in browser::

        async with NightWatch.acme.playwright() as browser:
            page = await browser.new_page()
            await page.goto("https://acme.example/home")
    """

    # Browsers run in containers on the Druks box, whatever provider the
    # installation uses.
    sandbox: ClassVar[Sandbox] = Sandbox(setup="sandboxes/browser.sh", provider="docker")
    site: str
    # Write the browser state back after each borrow — for sites that rotate
    # cookies on use, where a never-updated login ages out.
    persist: bool = False
    # Opt-in optimization for sites that don't fingerprint headless chromium.
    headless: bool = False
    # The session needs no login: every borrow opens a blank profile and the
    # operator is never asked to sign in.
    anonymous: bool = False
    name: str = field(init=False, default="")

    def __post_init__(self) -> None:
        if self.anonymous and self.persist:
            raise ValueError(
                "BrowserSession(anonymous=True, persist=True): an anonymous "
                "session has no state to write back. Drop persist=True."
            )

    def __set_name__(self, owner: type, attr: str) -> None:
        self.name = f"{owner.name}.{attr}"
        browser_sessions.register(self)

    @property
    def initial_status(self) -> BrowserSessionStatus:
        """The status a session holds before anyone acts on it: anonymous
        sessions never want a login."""
        if self.anonymous:
            return BrowserSessionStatus.ANONYMOUS
        return BrowserSessionStatus.NEEDS_LOGIN

    async def get_status(self) -> BrowserSessionStatus:
        """Where the login stands: READY to borrow, STALE after a run found it
        signed out, NEEDS_LOGIN before the first sign-in. Does not load the
        stored profile."""
        status = await StoredBrowserSession.get_status_for_name(db_session(), self.name)
        if status:
            return BrowserSessionStatus(status)
        return self.initial_status

    @asynccontextmanager
    async def cdp(self):
        """A browser carrying the session's login (blank for an anonymous
        session), reachable at the yielded CDP url for the length of the
        block. The browser lives in its own container on the druks box and
        dies with the block; a persisting session is exported and stored
        back first."""
        row = await (self.get_or_create_row() if self.anonymous else self._ready_row())
        writer_lock = AsyncExitStack()
        if self.persist:
            try:
                await writer_lock.enter_async_context(
                    lock(f"browser_session:{row.id}", blocking=False)
                )
            except LockHeldError as error:
                raise BrowserSessionWriterLockedError(row.id) from error
        async with writer_lock:
            settings = load_settings()
            template = await get_template_id(self.sandbox)
            async with sandbox_client.ephemeral(
                provider=self.sandbox.provider, template=template
            ) as browser:
                await seed_state(browser, row)
                await self._launch(browser, settings)
                await row.mark_used()
                listener = await browser.forward_local_port(CDP_PORT)
                try:
                    yield f"http://127.0.0.1:{listener.get_port()}"
                except BrowserSessionSignedOutError as error:
                    # The app says the login bounced; only the door knows
                    # which session that was. The run machinery does the rest.
                    error.session_name = self.name
                    raise
                finally:
                    listener.close()
                if self.persist:
                    row.payload_format = BrowserSessionPayloadFormat.PROFILE_DIR.value
                    await row.store_payload(await export_state(browser, self.name))

    @asynccontextmanager
    async def playwright(self):
        """The logged-in browser context, driven with playwright — the usual
        door. The login lives in this context, so pages opened on it
        (``await browser.new_page()``) are signed in. Playwright comes from
        the app's own dependencies; ``cdp()`` yields the raw url for any
        other client."""
        try:
            import playwright.async_api as playwright_api  # pyright: ignore[reportMissingImports]
        except ModuleNotFoundError as error:
            raise BrowserClientMissingError(self.name) from error
        async with self.cdp() as cdp_url, playwright_api.async_playwright() as driver:
            try:
                connection = await asyncio.wait_for(
                    driver.chromium.connect_over_cdp(cdp_url),
                    timeout=CDP_CONNECT_TIMEOUT_SECONDS,
                )
            except TimeoutError as error:
                raise BrowserLaunchError(
                    self.name, "timed out attaching to Chrome over CDP"
                ) from error
            try:
                yield await _playwright_context(connection, self.name)
            finally:
                await connection.close()

    async def get_or_create_row(self) -> StoredBrowserSession:
        """The declaration's stored half, written by the first action that
        needs it — a borrow, a login-window open, or a state import. Until
        then the declaration alone puts the session in the pane, wanting a
        login."""
        return await StoredBrowserSession.get_or_create(
            db_session(),
            name=self.name,
            payload_format=BrowserSessionPayloadFormat.PROFILE_DIR,
            site=self.site,
            status=self.initial_status,
        )

    async def _ready_row(self) -> StoredBrowserSession:
        row = await self.get_or_create_row()
        if row.status != BrowserSessionStatus.READY.value:
            raise BrowserSessionNotReadyError(self.name, row.status)
        return row

    async def _launch(self, browser, settings: Settings) -> None:
        env: dict[str, str] = {}
        if settings.browser.proxy_scope == "all":
            env = {
                "TZ": settings.browser.timezone,
                "DRUKS_BROWSER_PROXY": settings.browser.proxy.get_secret_value(),
            }
        await launch(browser, self.name, headless=self.headless, env=env)


async def seed_state(browser, row: StoredBrowserSession) -> None:
    """Put the row's stored browser state in the container before launch."""
    with tempfile.TemporaryDirectory(prefix="druks-browser-") as staging:
        if row.payload:
            state_filename = (
                "state.json"
                if row.payload_format == BrowserSessionPayloadFormat.STORAGE_STATE.value
                else "state.tar.gz"
            )
            state_path = Path(staging) / state_filename
            state_path.write_bytes(row.payload.decrypt())
            await browser.upload_file(local=state_path, remote=f"{SESSION_ROOT}/{state_filename}")
            metadata = {"format": row.payload_format, "version": 1}
        else:
            # Nothing stored — an anonymous session, or a login window opened
            # before the first sign-in. Version zero tells the launcher to
            # open a blank profile.
            metadata = {"format": BrowserSessionPayloadFormat.PROFILE_DIR.value, "version": 0}
        metadata_path = Path(staging) / "state.meta.json"
        metadata_path.write_text(json.dumps(metadata))
        await browser.upload_file(local=metadata_path, remote=f"{SESSION_ROOT}/state.meta.json")


async def launch(browser, name: str, *, headless: bool, env: dict[str, str]) -> None:
    """Start the launcher and wait for Chrome to report ready. ``env`` reaches the
    launcher's process; an empty value is left unset."""
    # The scripts ship with this code, so a change to them needs no template build.
    for script in ("session-launch", "session-export"):
        await browser.upload_file(
            local=Path(__file__).with_name(script), remote=f"{SESSION_ROOT}/{script}", mode=0o755
        )
    mode = "--headless" if headless else "--headed"
    assignments = " ".join(f"{key}={shlex.quote(value)}" for key, value in env.items() if value)
    env_prefix = f"env {assignments} " if assignments else ""
    ready = await browser.exec(
        [
            "sh",
            "-c",
            f"nohup setsid {env_prefix}{SESSION_ROOT}/session-launch {mode} "
            f">{SESSION_ROOT}/launch.log 2>&1 </dev/null & "
            'launcher=$!; attempt=0; while [ "$attempt" -lt 300 ]; do '
            f"if [ -f {SESSION_ROOT}/.runtime/ready.json ]; then exit 0; fi; "
            'if ! kill -0 "$launcher" 2>/dev/null; then '
            f"cat {SESSION_ROOT}/launch.log >&2; exit 1; fi; "
            "sleep 0.1; attempt=$((attempt + 1)); done; "
            "printf 'browser did not become ready\\n' >&2; exit 1",
        ],
        timeout=SESSION_LAUNCH_TIMEOUT_SECONDS,
    )
    if not ready.ok:
        raise BrowserLaunchError(name, ready.stderr.strip())


async def export_state(browser, name: str) -> bytes:
    """Close the browser and read back its profile archive."""
    exported = await browser.exec(
        [f"{SESSION_ROOT}/session-export"], timeout=SESSION_EXPORT_TIMEOUT_SECONDS
    )
    if not exported.ok:
        raise BrowserExportError(name, exported.stderr.strip())
    with tempfile.TemporaryDirectory(prefix="druks-browser-") as staging:
        exported_path = Path(staging) / "state.tar.gz"
        await browser.download(remote=f"{SESSION_ROOT}/out/state.tar.gz", local=exported_path)
        return exported_path.read_bytes()

import json
import tempfile
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

from druks.apps.registry import browser_sessions
from druks.browser.constants import (
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
)
from druks.browser.locks import acquire_writer_lock, release_writer_lock
from druks.browser.models import StoredBrowserSession
from druks.sandbox.client import sandbox_client
from druks.settings import load_settings

SESSION_ROOT = "/work/session"
CDP_PORT = 9222


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

    @asynccontextmanager
    async def cdp(self):
        """A browser carrying the session's login (blank for an anonymous
        session), reachable at the yielded CDP url for the length of the
        block. The browser lives in its own container on the druks box and
        dies with the block; a persisting session is exported and stored
        back first."""
        row = self.get_or_create_row() if self.anonymous else self._ready_row()
        writer_token = await acquire_writer_lock(row.id) if self.persist else ""
        try:
            settings = load_settings()
            async with sandbox_client.ephemeral(
                image_override=settings.sandbox.browser_sandbox_image,
                provider=settings.sandbox.browser_sandbox_provider,
            ) as browser:
                await seed_state(browser, row)
                await self._launch(browser)
                row.mark_used()
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
                    row.store_payload(await self._export(browser))
        finally:
            if writer_token:
                await release_writer_lock(row.id, writer_token)

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
            connection = await driver.chromium.connect_over_cdp(cdp_url)
            try:
                yield connection.contexts[0]
            finally:
                await connection.close()

    def get_or_create_row(self) -> StoredBrowserSession:
        """The declaration's stored half, written by the first action that
        needs it — a borrow, a login-window open, or a state import. Until
        then the declaration alone puts the session in the pane, wanting a
        login."""
        return StoredBrowserSession.get_or_create(
            name=self.name,
            payload_format=BrowserSessionPayloadFormat.PROFILE_DIR,
            site=self.site,
            status=self.initial_status,
        )

    def _ready_row(self) -> StoredBrowserSession:
        row = self.get_or_create_row()
        if row.status != BrowserSessionStatus.READY.value:
            raise BrowserSessionNotReadyError(self.name, row.status)
        return row

    async def _launch(self, browser) -> None:
        mode = "--headless" if self.headless else "--headed"
        ready = await browser.exec(
            [
                "sh",
                "-c",
                f"nohup setsid session-launch {mode} "
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
            raise BrowserLaunchError(self.name, ready.stderr.strip())

    async def _export(self, browser) -> bytes:
        exported = await browser.exec(["session-export"], timeout=SESSION_EXPORT_TIMEOUT_SECONDS)
        if not exported.ok:
            raise BrowserExportError(self.name, exported.stderr.strip())
        with tempfile.TemporaryDirectory(prefix="druks-browser-") as staging:
            exported_path = Path(staging) / "state.tar.gz"
            await browser.download(remote=f"{SESSION_ROOT}/out/state.tar.gz", local=exported_path)
            return exported_path.read_bytes()


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

import json
import tempfile
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

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
    BrowserSessionUnknownError,
)
from druks.browser.locks import acquire_writer_lock, release_writer_lock
from druks.browser.models import StoredBrowserSession
from druks.sandbox.client import sandbox_client
from druks.settings import load_settings

SESSION_ROOT = "/work/session"
CDP_PORT = 9222


@dataclass
class BrowserSession:
    """A named browser login the extension's runs borrow.

    Declared on the Extension class — ``x = BrowserSession(site="x.com")`` — the
    attribute name and the extension's name become the session's identity
    (``x_me.x``). The operator signs in once through the login window; a
    workflow then borrows the logged-in browser::

        async with XMe.x.playwright() as browser:
            page = await browser.new_page()
            await page.goto("https://x.com/home")
    """

    site: str
    # Write the browser state back after each borrow — for sites that rotate
    # cookies on use, where a never-updated login ages out.
    persist: bool = False
    # Opt-in optimization for sites that don't fingerprint headless chromium.
    headless: bool = False
    name: str = field(init=False, default="")

    def __set_name__(self, owner: type, attr: str) -> None:
        self.name = f"{owner.name}.{attr}"

    @asynccontextmanager
    async def cdp(self):
        """A logged-in browser, reachable at the yielded CDP url for the
        length of the block. The browser lives in its own container on the
        druks box and dies with the block; a persisting session is exported
        and stored back first."""
        row = self._ready_row()
        writer_token = await acquire_writer_lock(row.id) if self.persist else ""
        try:
            settings = load_settings()
            async with sandbox_client.ephemeral(
                image_override=settings.sandbox.browser_sandbox_image,
                provider=settings.sandbox.browser_sandbox_provider,
            ) as browser:
                await self._materialize(browser, row)
                await self._launch(browser)
                row.mark_used()
                listener = await browser.forward_local_port(CDP_PORT)
                try:
                    yield f"http://127.0.0.1:{listener.get_port()}"
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
        the extension's own dependencies; ``cdp()`` yields the raw url for any
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

    def mark_stale(self) -> None:
        """Report the login bounced — the site wants the operator back. The
        pane shows the session as stale; the workflow decides whether to park."""
        row = StoredBrowserSession.get_for_name(self.name)
        if not row:
            raise BrowserSessionUnknownError(self.name)
        row.mark_stale()

    def _ready_row(self) -> StoredBrowserSession:
        row = StoredBrowserSession.get_for_name(self.name)
        if not row:
            raise BrowserSessionUnknownError(self.name)
        if row.status != BrowserSessionStatus.READY.value:
            raise BrowserSessionNotReadyError(self.name, row.status)
        return row

    async def _materialize(self, browser, row: StoredBrowserSession) -> None:
        state_filename = (
            "state.json"
            if row.payload_format == BrowserSessionPayloadFormat.STORAGE_STATE.value
            else "state.tar.gz"
        )
        metadata = json.dumps({"format": row.payload_format, "version": 1})
        with tempfile.TemporaryDirectory(prefix="druks-browser-") as staging:
            state_path = Path(staging) / state_filename
            state_path.write_bytes(row.payload.decrypt())
            metadata_path = Path(staging) / "state.meta.json"
            metadata_path.write_text(metadata)
            await browser.upload_file(local=state_path, remote=f"{SESSION_ROOT}/{state_filename}")
            await browser.upload_file(local=metadata_path, remote=f"{SESSION_ROOT}/state.meta.json")

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

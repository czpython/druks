import asyncio
import json
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

import asyncssh
from fastapi import WebSocket
from sqlalchemy.ext.asyncio import AsyncSession

from druks.browser import exceptions
from druks.browser.constants import (
    LOGIN_WINDOW_KEY_PREFIX,
    LOGIN_WINDOW_TTL_SECONDS,
    SCREEN_CHUNK_BYTES,
    SESSION_EXPORT_TIMEOUT_SECONDS,
    VNC_PORT,
)
from druks.browser.enums import BrowserSessionPayloadFormat
from druks.browser.models import StoredBrowserSession
from druks.browser.sessions import SESSION_ROOT, launch, seed_state
from druks.redis import get_client
from druks.sandbox.client import sandbox_client
from druks.sandbox.host import Host
from druks.settings import load_settings


class LoginWindow:
    """The browser a user signs into by hand for one session. The operator
    opens it, watches it over a WebSocket, then saves or cancels — each a
    separate request, so it lives in Redis (and its container on the browser
    home) between them, and frees itself on the record's TTL if the operator
    walks away."""

    def __init__(self, session_name: str, host_id: str) -> None:
        self.session_name = session_name
        self.host_id = host_id

    @classmethod
    async def open(cls, session: StoredBrowserSession) -> "LoginWindow":
        stale = await get_client().get(_key(session.name))
        if stale:
            await cls(session.name, json.loads(stale)["host_id"])._close()
        settings = load_settings()
        try:
            browser = await sandbox_client.provision(
                image_override=settings.browser.sandbox_image,
                provider=settings.browser.sandbox_provider,
            )
        except Exception as error:
            raise exceptions.BrowserLaunchError(session.name, str(error)) from error
        try:
            await seed_state(browser, session)
            # Every proxy scope covers the login window, so it always gets the
            # proxy and TZ.
            await launch(
                browser,
                session.name,
                headless=False,
                env={
                    "TZ": settings.browser.timezone,
                    "DRUKS_BROWSER_PROXY": settings.browser.proxy.get_secret_value(),
                    "DRUKS_BROWSER_URL": f"https://{session.site}",
                },
            )
        except BaseException:
            await sandbox_client.release(host_id=browser.id)
            raise
        finally:
            await browser.aclose()
        await get_client().set(
            _key(session.name),
            json.dumps({"host_id": browser.id}),
            ex=LOGIN_WINDOW_TTL_SECONDS,
        )
        return cls(session.name, browser.id)

    @classmethod
    async def get_for_session(cls, session_name: str) -> "LoginWindow":
        """The window the operator has open for this session; raises once it is
        saved, cancelled, or aged out, which is every caller's cue to stop."""
        record = await get_client().get(_key(session_name))
        if record:
            return cls(session_name, json.loads(record)["host_id"])
        raise exceptions.BrowserLoginWindowGoneError

    async def stream(self, websocket: WebSocket) -> None:
        """Put the operator's canvas in front of the container's screen until
        either hangs up. The VNC port listens on loopback behind an
        authenticated SSH channel, so the two ends speak RFB to each other and
        nothing here reads a byte of it."""
        async with sandbox_client.attach(host_id=self.host_id) as browser:
            screen_reader, screen_writer = await browser.open_tcp_connection("127.0.0.1", VNC_PORT)
            await websocket.accept()
            directions = (
                asyncio.create_task(_carry_clicks(websocket, screen_writer)),
                asyncio.create_task(_carry_screen(websocket, screen_reader)),
            )
            try:
                await asyncio.wait(directions, return_when=asyncio.FIRST_COMPLETED)
            finally:
                # Whichever end hung up, the other has nobody left to talk to.
                for direction in directions:
                    direction.cancel()
                await asyncio.gather(*directions, return_exceptions=True)
                screen_writer.close()

    async def save(self, session: AsyncSession) -> StoredBrowserSession:
        """Store what the operator logged into as the session's payload, then
        tear the window down. A login always captures a profile, so a session
        imported as storage_state becomes a profile here."""
        row = await StoredBrowserSession.get_for_name(session, self.session_name)
        if row:
            try:
                async with sandbox_client.attach(host_id=self.host_id) as browser:
                    payload = await _export(browser, row.name)
                row.payload_format = BrowserSessionPayloadFormat.PROFILE_DIR.value
                await row.store_payload(payload)
                return row
            finally:
                await self._close()
        raise exceptions.BrowserSessionUnknownError(self.session_name)

    async def cancel(self) -> None:
        await self._close()

    async def _close(self) -> None:
        await sandbox_client.release(host_id=self.host_id)
        await get_client().delete(_key(self.session_name))


def _key(session_name: str) -> str:
    return f"{LOGIN_WINDOW_KEY_PREFIX}{session_name}"


def is_same_origin(websocket: WebSocket) -> bool:
    # The browser reaches Druks at urls.endpoint. An edge can rewrite Host to its
    # upstream address, so Host is the fallback only when no endpoint is set.
    # A TLS edge leaves us seeing ws while the browser's Origin says https, so
    # only the host is comparable — and it's the boundary that matters.
    endpoint = websocket.app.state.settings.urls.endpoint
    origins = websocket.headers.getlist("origin")
    hosts = [urlsplit(endpoint).netloc] if endpoint else websocket.headers.getlist("host")
    return (
        len(origins) == 1
        and len(hosts) == 1
        and urlsplit(origins[0]).netloc.lower() == hosts[0].lower()
    )


async def _carry_clicks(websocket: WebSocket, screen_writer: asyncssh.SSHWriter[bytes]) -> None:
    while (message := await websocket.receive())["type"] != "websocket.disconnect":
        if clicks := message.get("bytes"):
            screen_writer.write(clicks)
            await screen_writer.drain()


async def _carry_screen(websocket: WebSocket, screen_reader: asyncssh.SSHReader[bytes]) -> None:
    while pixels := await screen_reader.read(SCREEN_CHUNK_BYTES):
        await websocket.send_bytes(pixels)


async def _export(browser: Host, name: str) -> bytes:
    exported = await browser.exec(["session-export"], timeout=SESSION_EXPORT_TIMEOUT_SECONDS)
    if not exported.ok:
        raise exceptions.BrowserExportError(name, exported.stderr.strip())
    with tempfile.TemporaryDirectory(prefix="druks-browser-") as staging:
        out = Path(staging) / "state.tar.gz"
        await browser.download(remote=f"{SESSION_ROOT}/out/state.tar.gz", local=out)
        return out.read_bytes()

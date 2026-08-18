import asyncio
import json
import shlex
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

import asyncssh
from fastapi import WebSocket

from druks.browser import exceptions
from druks.browser.constants import (
    LOGIN_WINDOW_KEY_PREFIX,
    LOGIN_WINDOW_TTL_SECONDS,
    SCREEN_CHUNK_BYTES,
    SESSION_EXPORT_TIMEOUT_SECONDS,
    SESSION_LAUNCH_TIMEOUT_SECONDS,
    VNC_PORT,
)
from druks.browser.enums import BrowserSessionPayloadFormat
from druks.browser.models import StoredBrowserSession
from druks.browser.sessions import SESSION_ROOT
from druks.redis import get_client
from druks.sandbox.client import sandbox_client
from druks.sandbox.host import Sandbox
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
                image_override=settings.sandbox.browser_sandbox_image,
                provider=settings.sandbox.browser_sandbox_provider,
            )
        except Exception as error:
            raise exceptions.BrowserLaunchError(session.name, str(error)) from error
        try:
            await _seed(browser, session)
            await _launch(browser, session.name, settings.sandbox.browser_login_proxy)
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

    async def save(self) -> StoredBrowserSession:
        """Store what the operator logged into as the session's payload, then
        tear the window down. A login always captures a profile, so a session
        imported as storage_state becomes a profile here."""
        session = StoredBrowserSession.get_for_name(self.session_name)
        if session:
            try:
                async with sandbox_client.attach(host_id=self.host_id) as browser:
                    payload = await _export(browser, session.name)
                session.payload_format = BrowserSessionPayloadFormat.PROFILE_DIR.value
                session.store_payload(payload)
                return session
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
    # A TLS edge leaves us seeing ws while the browser's Origin says https, so
    # only the host is comparable — and it's the boundary that matters.
    origins = websocket.headers.getlist("origin")
    hosts = websocket.headers.getlist("host")
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


async def _seed(browser: Sandbox, session: StoredBrowserSession) -> None:
    with tempfile.TemporaryDirectory(prefix="druks-browser-") as staging:
        if session.payload:
            filename = (
                "state.json"
                if session.payload_format == BrowserSessionPayloadFormat.STORAGE_STATE.value
                else "state.tar.gz"
            )
            state = Path(staging) / filename
            state.write_bytes(session.payload.decrypt())
            await browser.upload_file(local=state, remote=f"{SESSION_ROOT}/{filename}")
            metadata = {"format": session.payload_format, "version": 1}
        else:
            # A session no one has logged into yet has nothing to unpack; version
            # zero tells the launcher to open a blank profile.
            metadata = {"format": BrowserSessionPayloadFormat.PROFILE_DIR.value, "version": 0}
        meta = Path(staging) / "state.meta.json"
        meta.write_text(json.dumps(metadata))
        await browser.upload_file(local=meta, remote=f"{SESSION_ROOT}/state.meta.json")


async def _launch(browser: Sandbox, name: str, login_proxy: str) -> None:
    # A login-only egress proxy: the launcher reads it from its environment.
    # Empty leaves the browser on the box's own IP.
    proxy_env = f"env DRUKS_BROWSER_LOGIN_PROXY={shlex.quote(login_proxy)} " if login_proxy else ""
    ready = await browser.exec(
        [
            "sh",
            "-c",
            f"nohup setsid {proxy_env}session-launch --headed "
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
        raise exceptions.BrowserLaunchError(name, ready.stderr.strip())


async def _export(browser: Sandbox, name: str) -> bytes:
    exported = await browser.exec(["session-export"], timeout=SESSION_EXPORT_TIMEOUT_SECONDS)
    if not exported.ok:
        raise exceptions.BrowserExportError(name, exported.stderr.strip())
    with tempfile.TemporaryDirectory(prefix="druks-browser-") as staging:
        out = Path(staging) / "state.tar.gz"
        await browser.download(remote=f"{SESSION_ROOT}/out/state.tar.gz", local=out)
        return out.read_bytes()

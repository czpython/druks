import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, WebSocket

from druks.accounts.dependencies import (
    current_account,
    current_session_account,
    require_operator,
)
from druks.accounts.models import Account
from druks.apps.registry import browser_sessions
from druks.browser import exceptions
from druks.browser.constants import MAX_PAYLOAD_BYTES, PAYLOAD_WARNING_BYTES
from druks.browser.enums import BrowserSessionPayloadFormat
from druks.browser.login import LoginWindow, is_same_origin
from druks.browser.models import StoredBrowserSession
from druks.browser.schemas import BrowserSessionResponse
from druks.database import session_scope

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/browser-sessions", tags=["browser-sessions"])


@router.get("", response_model=list[BrowserSessionResponse])
async def list_browser_sessions(account: Account = Depends(current_account)):
    rows = {row.name: row for row in StoredBrowserSession.list_all()}
    sessions = []
    for declaration in browser_sessions.all():
        try:
            row = rows.pop(declaration.name)
        except KeyError:
            sessions.append(
                {
                    "name": declaration.name,
                    "site": declaration.site,
                    "is_declared": True,
                    "status": declaration.initial_status,
                }
            )
        else:
            sessions.append(
                {
                    "name": declaration.name,
                    "site": declaration.site,
                    "is_declared": True,
                    "status": row.status,
                    "payload_format": row.payload_format,
                    "created_at": row.created_at,
                    "last_refreshed_at": row.last_refreshed_at,
                    "last_used_at": row.last_used_at,
                }
            )
    sessions.extend(rows.values())
    return sessions


@router.put("/{name}/state", status_code=204)
async def upload_state(
    name: str,
    payload_format: Annotated[BrowserSessionPayloadFormat, Query(alias="payloadFormat")],
    request: Request,
    account: Account = Depends(current_session_account),
) -> None:
    if declaration := browser_sessions.get(name):
        if declaration.anonymous:
            raise exceptions.BrowserSessionAnonymousError(name)
        row = declaration.get_or_create_row()
        payload = bytearray()
        async for chunk in request.stream():
            payload.extend(chunk)
            if len(payload) > MAX_PAYLOAD_BYTES:
                raise HTTPException(
                    status_code=413,
                    detail="Browser session payload exceeds the 256 MB limit.",
                )
        if len(payload) >= PAYLOAD_WARNING_BYTES:
            logger.warning("Browser session %s received a %d-byte payload.", name, len(payload))
        row.payload_format = payload_format.value
        row.store_payload(bytes(payload))
        return
    raise exceptions.BrowserSessionUnknownError(name)


@router.post("/{name}/login-window", status_code=204)
async def open_login_window(
    name: str,
    account: Account = Depends(current_session_account),
) -> None:
    if declaration := browser_sessions.get(name):
        if declaration.anonymous:
            raise exceptions.BrowserSessionAnonymousError(name)
        await LoginWindow.open(declaration.get_or_create_row())
        return
    raise exceptions.BrowserSessionUnknownError(name)


@router.websocket("/{name}/login-window/ws")
async def login_window_socket(websocket: WebSocket, name: str) -> None:
    if not is_same_origin(websocket):
        await websocket.close(code=1008)
        return
    try:
        with session_scope(websocket.app.state.engine):
            await require_operator(websocket)
        window = await LoginWindow.get_for_session(name)
    except (HTTPException, exceptions.BrowserApiError):
        await websocket.close(code=1008)
        return
    try:
        await window.stream(websocket)
    except Exception:
        await websocket.close(code=1011)


@router.post("/{name}/login-window/save", status_code=204)
async def save_login_window(
    name: str,
    account: Account = Depends(current_session_account),
) -> None:
    window = await LoginWindow.get_for_session(name)
    await window.save()


@router.post("/{name}/login-window/cancel", status_code=204)
async def cancel_login_window(
    name: str,
    account: Account = Depends(current_session_account),
) -> None:
    window = await LoginWindow.get_for_session(name)
    await window.cancel()


@router.delete("/{name}", status_code=204)
async def delete_browser_session(
    name: str,
    account: Account = Depends(current_session_account),
) -> None:
    if row := StoredBrowserSession.get_for_name(name):
        row.delete()
        return
    raise exceptions.BrowserSessionUnknownError(name)

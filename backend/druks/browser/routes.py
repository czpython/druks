import logging
from typing import Annotated

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from sqlalchemy.exc import IntegrityError

from druks.accounts.dependencies import current_account, current_session_account
from druks.accounts.models import Account
from druks.browser.constants import (
    BROWSER_SESSION_NAME_MAX_LENGTH,
    BROWSER_SESSION_NAME_PATTERN,
    MAX_PAYLOAD_BYTES,
    PAYLOAD_WARNING_BYTES,
)
from druks.browser.models import StoredBrowserSession
from druks.browser.schemas import BrowserSessionResponse, CreateBrowserSessionRequest
from druks.database import db_session

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/browser-sessions", tags=["browser-sessions"])


@router.get("", response_model=list[BrowserSessionResponse])
async def list_browser_sessions(
    account: Account = Depends(current_account),
) -> list[StoredBrowserSession]:
    return StoredBrowserSession.list_all()


@router.post("", response_model=BrowserSessionResponse)
async def create_browser_session(
    body: CreateBrowserSessionRequest,
    account: Account = Depends(current_session_account),
) -> StoredBrowserSession:
    try:
        return StoredBrowserSession.create(
            name=body.name,
            payload_format=body.payload_format,
            site=body.site,
        )
    except IntegrityError as error:
        db_session().rollback()
        raise HTTPException(
            status_code=409,
            detail=f"Browser session {body.name!r} already exists.",
        ) from error


@router.get("/{session_id}", response_model=BrowserSessionResponse)
async def get_browser_session(
    session_id: str,
    account: Account = Depends(current_account),
) -> StoredBrowserSession:
    browser_session = StoredBrowserSession.get_for_id(session_id)
    if browser_session:
        return browser_session
    raise HTTPException(status_code=404, detail="Browser session not found.")


@router.put("/{session_id}/state", response_model=BrowserSessionResponse)
async def upload_state(
    session_id: str,
    request: Request,
    account: Account = Depends(current_session_account),
) -> StoredBrowserSession:
    browser_session = StoredBrowserSession.get_for_id(session_id)
    if not browser_session:
        raise HTTPException(status_code=404, detail="Browser session not found.")
    payload = bytearray()
    async for chunk in request.stream():
        payload.extend(chunk)
        if len(payload) > MAX_PAYLOAD_BYTES:
            raise HTTPException(
                status_code=413,
                detail="Browser session payload exceeds the 256 MB limit.",
            )
    if len(payload) >= PAYLOAD_WARNING_BYTES:
        logger.warning(
            "Browser session %s received a %d-byte payload.",
            session_id,
            len(payload),
        )
    browser_session.store_payload(bytes(payload))
    return browser_session


@router.patch("/{session_id}", response_model=BrowserSessionResponse)
async def rename_browser_session(
    session_id: str,
    name: Annotated[
        str,
        Body(
            embed=True,
            min_length=1,
            max_length=BROWSER_SESSION_NAME_MAX_LENGTH,
            pattern=BROWSER_SESSION_NAME_PATTERN,
        ),
    ],
    account: Account = Depends(current_session_account),
) -> StoredBrowserSession:
    browser_session = StoredBrowserSession.get_for_id(session_id)
    if not browser_session:
        raise HTTPException(status_code=404, detail="Browser session not found.")
    try:
        browser_session.rename(name)
    except IntegrityError as error:
        db_session().rollback()
        raise HTTPException(
            status_code=409,
            detail=f"Browser session {name!r} already exists.",
        ) from error
    return browser_session


@router.delete("/{session_id}", status_code=204)
async def delete_browser_session(
    session_id: str,
    account: Account = Depends(current_session_account),
) -> None:
    browser_session = StoredBrowserSession.get_for_id(session_id)
    if not browser_session:
        raise HTTPException(status_code=404, detail="Browser session not found.")
    browser_session.delete()

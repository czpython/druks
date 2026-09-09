import asyncio
from datetime import UTC
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from pydantic import AwareDatetime

from druks.api.dependencies import EngineDep
from druks.database import db_session, session_scope
from druks.durable.live import SSE_HEADERS
from druks.events.builder import build_feed
from druks.events.feed import FeedResponse
from druks.events.models import Event

router = APIRouter(prefix="/api/events", tags=["feed"])

_SSE_POLL_INTERVAL_SECONDS = 2.0


def _parse_cursor(raw: str | None) -> int | None:
    if raw is None:
        return
    try:
        cursor = int(raw)
        if cursor < 0:
            raise ValueError
        return cursor
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid event cursor: {raw!r}. Use a returned sequence.",
        ) from error


def get_filters(
    app: str | None = Query(default=None),
    q: str | None = Query(default=None),
    kind: str | None = Query(default=None),
    from_at: Annotated[AwareDatetime | None, Query(alias="from")] = None,
    until: AwareDatetime | None = Query(default=None),
) -> dict:
    if from_at and until and from_at >= until:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "until must be after from.")
    return {
        "app": app,
        "q": q,
        "kind": kind,
        "from_at": from_at.astimezone(UTC) if from_at else None,
        "until": until.astimezone(UTC) if until else None,
    }


@router.get(
    "", response_model=FeedResponse, response_model_by_alias=True, response_model_exclude_unset=True
)
async def list_feed(
    filters: Annotated[dict, Depends(get_filters)],
    limit: int = Query(default=200, ge=1, le=500),
    before: str | None = Query(default=None),
) -> FeedResponse:
    cursor = _parse_cursor(before)
    items, next_cursor = await build_feed(**filters, before=cursor, limit=limit)
    response = FeedResponse(items=items, next_cursor=next_cursor)
    if before is None:
        statement = (
            Event.get_history(app=filters["app"])
            .with_only_columns(Event.type)
            .distinct()
            .order_by(Event.type)
        )
        response.kinds = list(await db_session().scalars(statement))
    return response


@router.get("/stream")
async def stream_feed(
    request: Request,
    engine: EngineDep,
    filters: Annotated[dict, Depends(get_filters)],
    after: str | None = Query(default=None),
) -> StreamingResponse:
    last_seq = _parse_cursor(request.headers.get("last-event-id") or after)

    async def feed_stream():
        nonlocal last_seq
        while True:
            if await request.is_disconnected():
                return
            async with session_scope(engine):
                items, cursor = await build_feed(**filters, after=last_seq, limit=100)
                # Catch-up can span several pages. Finish the interval before advancing its head.
                while last_seq is not None and cursor:
                    older, cursor = await build_feed(
                        **filters, after=last_seq, before=int(cursor), limit=100
                    )
                    items.extend(older)
            for item in reversed(items):
                yield f"id: {item.seq}\ndata: {item.model_dump_json(by_alias=True)}\n\n"
            if items:
                last_seq = items[0].seq
            try:
                await asyncio.sleep(_SSE_POLL_INTERVAL_SECONDS)
            except asyncio.CancelledError:
                return

    return StreamingResponse(
        feed_stream(),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )

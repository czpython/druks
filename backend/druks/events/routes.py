import asyncio

from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse

from druks.api.dependencies import EngineDep
from druks.database import session_scope
from druks.durable.live import SSE_HEADERS
from druks.events.builder import build_feed
from druks.events.feed import FeedResponse

router = APIRouter(prefix="/api/events", tags=["feed"])

# Per-connection SSE poll cadence. Short enough that the operator's screen feels
# live, long enough that we're not hammering the DB; the cost is one bounded
# read per tick, so cadence is set by perceived latency rather than load.
_SSE_POLL_INTERVAL_SECONDS = 2.0


def _parse_cursor(raw: str | None) -> int | None:
    # The cursor is a feed sequence (an event's monotonic pk), opaque to the client —
    # it hands back whatever ``next_cursor`` returned.
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid ``before`` cursor: {raw!r}",
        ) from exc


@router.get("", response_model=FeedResponse, response_model_by_alias=True)
async def list_feed(
    limit: int = Query(default=200, ge=1, le=500),
    before: str | None = Query(default=None),
    app: str | None = Query(default=None),
) -> FeedResponse:
    cursor = _parse_cursor(before)
    items, next_cursor = build_feed(app=app, before=cursor, limit=limit)
    return FeedResponse(items=items, next_cursor=next_cursor)


@router.get("/stream")
async def stream_feed(
    request: Request,
    engine: EngineDep,
    app: str | None = Query(default=None),
) -> StreamingResponse:
    async def feed_stream():
        last_seq: int | None = None
        first = True
        while True:
            if await request.is_disconnected():
                return
            # New Session per tick so we don't hold a transaction open across the
            # sleep; the open/close cost is irrelevant against the poll cadence.
            with session_scope(engine):
                items, _next_cursor = build_feed(
                    app=app,
                    before=None,
                    limit=100 if first else 50,
                )
            # Strictly past the last emitted sequence — the monotonic pk never ties,
            # so this neither re-sends the boundary event nor drops a same-second one.
            fresh = items if last_seq is None else [e for e in items if e.seq > last_seq]
            for item in reversed(fresh):  # oldest-first within a tick
                yield f"data: {item.model_dump_json(by_alias=True)}\n\n"
            if fresh:
                last_seq = fresh[0].seq  # newest just-emitted (page is seq-desc)
            first = False
            try:
                await asyncio.sleep(_SSE_POLL_INTERVAL_SECONDS)
            except asyncio.CancelledError:
                return

    return StreamingResponse(
        feed_stream(),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )

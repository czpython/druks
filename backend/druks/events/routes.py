import asyncio
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from pydantic import AwareDatetime

from druks.api.dependencies import EngineDep, SessionDep
from druks.database import session_scope
from druks.durable.live import SSE_HEADERS
from druks.events import reads
from druks.events.feed import FeedDestinations, FeedItem, FeedResponse
from druks.events.models import Event

router = APIRouter(prefix="/api/events", tags=["feed"])

_SSE_POLL_INTERVAL_SECONDS = 2.0
_SSE_PAGE_SIZE = 100


def _parse_cursor(raw: str | None) -> int | None:
    if raw is None:
        return
    try:
        cursor = int(raw)
        if cursor < 1:
            raise ValueError
        return cursor
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid event cursor: {raw!r}. Use a returned sequence.",
        ) from error


def _check_range(from_at: AwareDatetime | None, until: AwareDatetime | None) -> None:
    if from_at and until and from_at >= until:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"until {until.isoformat()} is not after from {from_at.isoformat()}. "
            "Send a later until.",
        )


@router.get("", response_model=FeedResponse, response_model_by_alias=True)
async def list_feed(
    session: SessionDep,
    app: Annotated[str | None, Query()] = None,
    search: Annotated[str | None, Query(alias="q")] = None,
    kind: Annotated[str | None, Query()] = None,
    from_at: Annotated[AwareDatetime | None, Query(alias="from")] = None,
    until: Annotated[AwareDatetime | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
    before: Annotated[str | None, Query()] = None,
) -> FeedResponse:
    _check_range(from_at, until)
    history = Event.get_history(app=app, search=search, kind=kind, from_at=from_at, until=until)
    cursor = _parse_cursor(before)
    if cursor:
        history = history.where(Event.id < cursor)
    events = list(await session.scalars(history.order_by(Event.id.desc()).limit(limit + 1)))
    next_cursor = str(events[limit - 1].id) if len(events) > limit else None
    return FeedResponse.model_validate({"items": events[:limit], "next_cursor": next_cursor})


@router.get("/kinds", response_model=list[str])
async def list_feed_kinds(
    session: SessionDep, app: Annotated[str | None, Query()] = None
) -> list[str]:
    return await reads.list_kinds(session, app)


@router.get("/{seq}/destinations", response_model=FeedDestinations, response_model_by_alias=True)
async def get_feed_destinations(session: SessionDep, seq: int) -> FeedDestinations:
    event = await session.scalar(Event.get_history().where(Event.id == seq))
    if not event:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, f"No Activity event {seq}. Use a seq from the feed."
        )
    return await reads.get_destinations(session, event)


@router.get("/stream")
async def stream_feed(
    request: Request,
    engine: EngineDep,
    app: Annotated[str | None, Query()] = None,
    search: Annotated[str | None, Query(alias="q")] = None,
    kind: Annotated[str | None, Query()] = None,
    from_at: Annotated[AwareDatetime | None, Query(alias="from")] = None,
    until: Annotated[AwareDatetime | None, Query()] = None,
    after: Annotated[str | None, Query()] = None,
) -> StreamingResponse:
    _check_range(from_at, until)
    history = Event.get_history(app=app, search=search, kind=kind, from_at=from_at, until=until)
    last_seq = _parse_cursor(request.headers.get("last-event-id") or after)

    async def feed_stream():
        nonlocal last_seq
        while not await request.is_disconnected():
            # Without a cursor the stream opens on the newest page; with one it reads forward.
            if last_seq:
                statement = history.where(Event.id > last_seq).order_by(Event.id)
            else:
                statement = history.order_by(Event.id.desc())
            async with session_scope(engine) as session:
                events = await session.scalars(statement.limit(_SSE_PAGE_SIZE))
                items = [FeedItem.model_validate(event) for event in events]
            if not last_seq:
                items.reverse()
            for item in items:
                yield f"id: {item.seq}\ndata: {item.model_dump_json(by_alias=True)}\n\n"
            if items:
                last_seq = items[-1].seq
            if len(items) < _SSE_PAGE_SIZE:
                try:
                    await asyncio.sleep(_SSE_POLL_INTERVAL_SECONDS)
                except asyncio.CancelledError:
                    return

    return StreamingResponse(
        feed_stream(),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )

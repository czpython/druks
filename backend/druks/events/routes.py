import asyncio
import json
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from pydantic import AwareDatetime
from sqlalchemy import text
from sqlalchemy.exc import DataError

from druks.api.dependencies import EngineDep, SessionDep
from druks.database import session_scope
from druks.durable.live import SSE_HEADERS
from druks.events import reads
from druks.events.feed import FeedDestinations, FeedItem, FeedResponse
from druks.events.models import Event

router = APIRouter(prefix="/api/events", tags=["feed"])

_SSE_POLL_INTERVAL_SECONDS = 2.0
_SSE_PAGE_SIZE = 100


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
    topic: Annotated[str | None, Query()] = None,
    from_at: Annotated[AwareDatetime | None, Query(alias="from")] = None,
    until: Annotated[AwareDatetime | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
    before: Annotated[str | None, Query()] = None,
) -> FeedResponse:
    _check_range(from_at, until)
    history = Event.get_history(app=app, search=search, topic=topic, from_at=from_at, until=until)
    if before is not None:
        try:
            sequence = int(before)
            if sequence < 1:
                raise ValueError
        except ValueError as error:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"Invalid event cursor: {before!r}. Use a returned sequence.",
            ) from error
        history = history.where(Event.id < sequence)
    events, snapshot = await reads.list_events(
        session, history.order_by(Event.id.desc()).limit(limit + 1)
    )
    next_cursor = str(events[limit - 1].id) if len(events) > limit else None
    return FeedResponse.model_validate(
        {"items": events[:limit], "next_cursor": next_cursor, "stream_cursor": snapshot}
    )


@router.get("/topics")
async def list_feed_topics(
    session: SessionDep, app: Annotated[str | None, Query()] = None
) -> list[dict[str, str]]:
    return await reads.list_topics(session, app)


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
    topic: Annotated[str | None, Query()] = None,
    from_at: Annotated[AwareDatetime | None, Query(alias="from")] = None,
    until: Annotated[AwareDatetime | None, Query()] = None,
    after: Annotated[str | None, Query()] = None,
) -> StreamingResponse:
    _check_range(from_at, until)
    history = Event.get_history(app=app, search=search, topic=topic, from_at=from_at, until=until)
    cursor = request.headers.get("last-event-id") or after
    if cursor:
        try:
            async with session_scope(engine) as session:
                await session.execute(
                    text("SELECT CAST(:cursor AS pg_snapshot)").bindparams(cursor=cursor)
                )
        except DataError as error:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "Invalid Activity snapshot. Use streamCursor from the history response.",
            ) from error

    async def feed_stream():
        nonlocal cursor
        while not await request.is_disconnected():
            if cursor:
                statement = history.where(
                    text(
                        "events.xid >= pg_snapshot_xmin(CAST(:cursor AS pg_snapshot)) "
                        "AND NOT pg_visible_in_snapshot(events.xid, CAST(:cursor AS pg_snapshot))"
                    ).bindparams(cursor=cursor)
                )
            else:
                statement = history.order_by(Event.id.desc()).limit(_SSE_PAGE_SIZE)
            async with session_scope(engine) as session:
                events, snapshot = await reads.list_events(session, statement)
                items = [FeedItem.model_validate(event) for event in reversed(events)]
            for item in items:
                yield f"data: {item.model_dump_json(by_alias=True)}\n\n"
            yield f"event: batch-end\nid: {snapshot}\ndata: {json.dumps({'cursor': snapshot})}\n\n"
            cursor = snapshot
            await asyncio.sleep(_SSE_POLL_INTERVAL_SECONDS)

    return StreamingResponse(
        feed_stream(),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )

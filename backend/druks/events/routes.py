import asyncio
import json
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from pydantic import AwareDatetime
from sqlalchemy.exc import DataError

from druks.api.dependencies import EngineDep, SessionDep
from druks.database import session_scope
from druks.durable.live import SSE_HEADERS
from druks.events import reads
from druks.events.feed import FeedDestinations, FeedItem, FeedResponse
from druks.events.models import Event

router = APIRouter(prefix="/api/events", tags=["feed"])

_SSE_POLL_INTERVAL_SECONDS = 2.0


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
    before: Annotated[int | None, Query(ge=1)] = None,
) -> FeedResponse:
    _check_range(from_at, until)
    history = Event.get_history(app=app, search=search, topic=topic, from_at=from_at, until=until)
    if before:
        history = history.where(Event.id < before)
    # The cursor comes first. A row that commits between the two reads lands in this
    # page and in the stream, and the page keeps one copy. The other order loses it.
    cursor = await Event.get_cursor(session)
    events = list(await session.scalars(history.order_by(Event.id.desc()).limit(limit + 1)))
    next_cursor = str(events[limit - 1].id) if len(events) > limit else None
    return FeedResponse.model_validate(
        {"items": events[:limit], "cursor": cursor, "next_cursor": next_cursor}
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
    cursor: Annotated[str | None, Query()] = None,
) -> StreamingResponse:
    _check_range(from_at, until)
    history = Event.get_history(app=app, search=search, topic=topic, from_at=from_at, until=until)
    try:
        async with session_scope(engine) as session:
            cursor = await Event.get_cursor(session, request.headers.get("last-event-id") or cursor)
    except DataError as error:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Invalid Activity cursor. Use the cursor from the history response.",
        ) from error

    async def feed_stream():
        nonlocal cursor
        while not await request.is_disconnected():
            async with session_scope(engine) as session:
                next_cursor = await Event.get_cursor(session)
                events = await session.scalars(
                    history.where(Event.committed_after(cursor)).order_by(Event.id)
                )
                items = [FeedItem.model_validate(event) for event in events]
            for item in items:
                yield f"data: {item.model_dump_json(by_alias=True)}\n\n"
            cursor = next_cursor
            yield f"event: batch-end\nid: {cursor}\ndata: {json.dumps({'cursor': cursor})}\n\n"
            await asyncio.sleep(_SSE_POLL_INTERVAL_SECONDS)

    return StreamingResponse(
        feed_stream(),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )

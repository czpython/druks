import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from pydantic import BaseModel

DEFAULT_POLL_INTERVAL_SECONDS = 2.0

# no-store so a proxy never replays a stale snapshot; X-Accel-Buffering off so nginx
# forwards each event instead of buffering the stream.
SSE_HEADERS = {"Cache-Control": "no-store", "X-Accel-Buffering": "no"}


def serialize_event(event_type: str, data: dict[str, Any]) -> str:
    payload = json.dumps(data, separators=(",", ":"), sort_keys=True)
    return f"event: {event_type}\ndata: {payload}\n\n"


def keepalive_comment() -> str:
    return ": ping\n\n"


def serialize_model_event(event_type: str, model: BaseModel) -> str:
    """Serialize a typed model as a named SSE event — the streaming twin of returning a
    Schema. Push events with this instead of hand-writing model_dump(by_alias)."""
    return serialize_event(event_type, model.model_dump(by_alias=True, mode="json"))


async def stream(
    snapshot: Callable[[], Awaitable[BaseModel | None]],
    *,
    poll_interval: float = DEFAULT_POLL_INTERVAL_SECONDS,
) -> AsyncIterator[str]:
    # Poll ``snapshot`` and re-emit the whole thing as a ``snapshot`` event
    # whenever it changes — one cheap whole-payload compare, no per-entity
    # diffing. The client renders the latest snapshot it receives; keepalive
    # comments cover unchanged ticks so proxies don't drop the idle connection.
    # Ends when snapshot() returns None (the subject is gone) or the client
    # disconnects.
    previous: str | None = None
    while True:
        current = await snapshot()
        if current is None:
            return
        data = current.model_dump(by_alias=True, mode="json")
        serialized = json.dumps(data, separators=(",", ":"), sort_keys=True)
        if serialized != previous:
            previous = serialized
            yield serialize_event("snapshot", data)
        else:
            yield keepalive_comment()
        try:
            await asyncio.sleep(poll_interval)
        except asyncio.CancelledError:
            return

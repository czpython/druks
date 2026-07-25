from datetime import datetime
from typing import Any

from pydantic import Field

from druks.events.models import Event
from druks.schemas import BaseResponse


class FeedItem(BaseResponse):
    id: str
    # The event's monotonic log position (its pk) — the feed's ordering and
    # pagination key. ``at`` is whole-second and ties constantly; this never does.
    # The builder stamps it from the Event, so an extension's format_event needn't.
    seq: int = 0
    at: datetime
    # Dotted ``source.phase`` taxonomy. Open-ended — the frontend keys pill
    # color off the prefix, so a new type lands in the right bucket for free.
    kind: str
    # Coarse source label for the feed's source column (linear / github / codex
    # / claude / worker / cron / watch).
    source: str
    summary: str
    # Internal route the row deep-links to; None when it has no detail page.
    link_path: str | None = None
    # Free-form secondary-line metadata; the frontend renders known keys and
    # ignores the rest, so new keys ship without a frontend change.
    meta: dict[str, Any] = Field(default_factory=dict)


class FeedResponse(BaseResponse):
    items: list[FeedItem]
    # Event sequence cursor for the next (older) page; None at the tail.
    next_cursor: str | None = None


def generic_entry(event: Event) -> FeedItem:
    """Fallback when an extension has no opinion — show the raw type."""
    return FeedItem(
        id=f"event:{event.id}",
        at=event.created_at,
        kind=event.type,
        source="system",
        summary=event.type,
    )

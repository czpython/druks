from datetime import datetime
from typing import Any

from pydantic import Field

from druks.events.models import Event
from druks.schemas import BaseResponse


class FeedItem(BaseResponse):
    id: str
    # The event's monotonic log position (its pk) — the feed's ordering and
    # pagination key. ``at`` is whole-second and ties constantly; this never does.
    seq: int
    at: datetime
    # The event type verbatim: a lifecycle topic ("workflow.finished") or the
    # milestone an extension recorded ("shipped"). The words are the client's.
    kind: str
    extension: str | None = None
    # The durable kind of the workflow a lifecycle row is about ("ship.build").
    workflow: str | None = None
    subject_type: str | None = None
    subject_id: str | None = None
    subject_label: str | None = None
    # Whatever else the event recorded, stated by its writer — a gate, a failure,
    # a ticket key. What a row carries beyond identity is said at write time.
    facts: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_event(cls, event: Event) -> "FeedItem":
        facts = dict(event.payload)
        return cls(
            id=f"event:{event.id}",
            seq=event.id,
            at=event.created_at,
            kind=event.type,
            extension=event.extension,
            workflow=facts.pop("kind", None),
            subject_type=event.subject_type,
            subject_id=event.subject_id,
            subject_label=event.subject_label,
            facts=facts,
        )


class FeedResponse(BaseResponse):
    items: list[FeedItem]
    # Event sequence cursor for the next (older) page; None at the tail.
    next_cursor: str | None = None

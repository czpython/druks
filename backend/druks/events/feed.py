from datetime import datetime
from typing import Any

from pydantic import ConfigDict, Field, computed_field

from druks.schemas import Schema


class FeedItem(Schema):
    model_config = ConfigDict(from_attributes=True)

    # The event's monotonic log position (its pk) — the feed's ordering and
    # pagination key. ``at`` is whole-second and ties constantly; this never does.
    seq: int = Field(validation_alias="id")
    at: datetime = Field(validation_alias="created_at")
    # The event type verbatim: a lifecycle topic ("workflow.finished") or the
    # milestone an app recorded ("merged"). The words are the client's.
    topic: str = Field(validation_alias="type")
    app: str | None = None
    subject_type: str | None = None
    subject_id: str | None = None
    subject_label: str | None = None
    payload: dict[str, Any]

    @computed_field
    @property
    def id(self) -> str:
        return f"event:{self.seq}"


class FeedResponse(Schema):
    items: list[FeedItem]
    stream_cursor: str
    # Event sequence cursor for the next (older) page; None at the tail.
    next_cursor: str | None = None


class FeedDestinations(Schema):
    is_subject_available: bool
    is_run_available: bool
    is_artifact_available: bool

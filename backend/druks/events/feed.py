from datetime import datetime

from pydantic import AliasPath, ConfigDict, Field, computed_field

from druks.schemas import BaseResponse


class FeedItem(BaseResponse):
    model_config = ConfigDict(from_attributes=True)

    # The event's monotonic log position (its pk) — the feed's ordering and
    # pagination key. ``at`` is whole-second and ties constantly; this never does.
    seq: int = Field(validation_alias="id")
    at: datetime = Field(validation_alias="created_at")
    # The event type verbatim: a lifecycle topic ("workflow.finished") or the
    # milestone an extension recorded ("merged"). The words are the client's.
    kind: str = Field(validation_alias="type")
    extension: str | None = None
    # The durable kind of the workflow a lifecycle row is about ("ship.build").
    workflow: str | None = Field(default=None, validation_alias=AliasPath("payload", "kind"))
    subject_type: str | None = None
    subject_id: str | None = None
    subject_label: str | None = None

    @computed_field
    @property
    def id(self) -> str:
        return f"event:{self.seq}"


class FeedResponse(BaseResponse):
    items: list[FeedItem]
    # Event sequence cursor for the next (older) page; None at the tail.
    next_cursor: str | None = None

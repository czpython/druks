from datetime import datetime
from typing import Any

from pydantic import AliasChoices, AliasPath, ConfigDict, Field, computed_field

from druks.schemas import Schema


class FeedItem(Schema):
    model_config = ConfigDict(from_attributes=True)

    # The event's monotonic log position (its pk) — the feed's ordering and
    # pagination key. ``at`` is whole-second and ties constantly; this never does.
    seq: int = Field(validation_alias="id")
    at: datetime = Field(validation_alias="created_at")
    # The event type verbatim: a lifecycle topic ("workflow.finished") or the
    # milestone an app recorded ("merged"). The words are the client's.
    kind: str = Field(validation_alias="type")
    app: str | None = None
    # The durable kind of the workflow a lifecycle row is about ("software_factory.build").
    workflow: str | None = Field(default=None, validation_alias=AliasPath("payload", "kind"))
    subject_type: str | None = None
    subject_id: str | None = None
    subject_label: str | None = None
    run: str | None = Field(default=None, validation_alias=AliasPath("payload", "run"))
    gate: str | None = Field(default=None, validation_alias=AliasPath("payload", "gate"))
    parked_at: datetime | None = Field(
        default=None, validation_alias=AliasPath("payload", "input_requested_at")
    )
    input_request: dict[str, Any] | None = Field(
        default=None, validation_alias=AliasPath("payload", "input_request")
    )
    result: Any = Field(default=None, validation_alias=AliasPath("payload", "result"))
    summary: str | None = Field(default=None, validation_alias=AliasPath("payload", "summary"))
    reason: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            AliasPath("payload", "reason"), AliasPath("payload", "failure")
        ),
    )
    artifact_id: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            AliasPath("payload", "artifact_id"),
            AliasPath("payload", "input_request", "artifact_id"),
        ),
    )
    agent_call_id: str | None = Field(
        default=None, validation_alias=AliasPath("payload", "agent_call_id")
    )
    is_subject_available: bool = False
    is_run_available: bool = False
    is_artifact_available: bool = False

    @computed_field
    @property
    def id(self) -> str:
        return f"event:{self.seq}"


class FeedResponse(Schema):
    items: list[FeedItem]
    # Event sequence cursor for the next (older) page; None at the tail.
    next_cursor: str | None = None
    kinds: list[str] | None = None

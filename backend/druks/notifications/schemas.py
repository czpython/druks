from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from druks.notifications.models import DestinationKind
from druks.schemas import Schema


class DestinationResponse(Schema):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    kind: str
    channel_id: str
    is_enabled: bool


class CreateDestinationRequest(BaseModel):
    name: str
    kind: DestinationKind
    channel_id: str = Field(alias="channelId")


class NotificationResponse(Schema):
    # Serializes exactly the fields declared here — the correlation token (the
    # respond capability) is deliberately not among them.
    model_config = ConfigDict(from_attributes=True)

    id: str
    subject: dict[str, Any]
    reason: str
    body: str
    actions: list[dict[str, Any]] | None
    destination_id: str
    state: str
    attempts: int
    last_error: str | None
    delivered_at: datetime | None
    created_at: datetime
    run_id: str | None
    run_parked_at: datetime | None
    deep_link: str | None


class RespondRequest(BaseModel):
    # Mirrors the runs resume request: a control the ask offered, an answer per
    # question, an optional free-text note.
    model_config = ConfigDict(str_strip_whitespace=True)

    control: str
    answers: dict[str, str] = Field(default_factory=dict)
    note: str = ""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, SecretStr, computed_field

from druks.notifications.models import DestinationKind
from druks.schemas import Schema


class DestinationResponse(Schema):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    kind: str
    # The stored URL is the credential: SecretStr serializes as the mask, and
    # has_url is the only signal the frontend needs.
    url: SecretStr
    is_enabled: bool

    @computed_field
    @property
    def has_url(self) -> bool:
        return bool(self.url.get_secret_value())


class CreateDestinationRequest(BaseModel):
    name: str
    # Unknown kinds are rejected by the enum; the URL is write-only, unwrapped
    # exactly once, to persist. Whether it actually reaches anything surfaces
    # at delivery — per-kind URL shapes are not the wire schema's business.
    kind: DestinationKind
    url: SecretStr


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

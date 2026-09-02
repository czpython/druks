from datetime import datetime

from pydantic import ConfigDict, Field, field_validator

from druks.schemas import Schema


class ProviderResponse(Schema):
    model_config = ConfigDict(from_attributes=True)

    id: str
    label: str
    login_kinds: list[str]

    @field_validator("login_kinds", mode="before")
    @classmethod
    def _sorted(cls, kinds: frozenset[str]) -> list[str]:
        return sorted(kinds)


class ProviderLoginResponse(Schema):
    model_config = ConfigDict(from_attributes=True)

    provider: str
    kind: str
    # The email the provider reported at connect — display, never authority.
    provider_email: str
    expires_at: datetime | None
    connected: bool = Field(validation_alias="is_connected")

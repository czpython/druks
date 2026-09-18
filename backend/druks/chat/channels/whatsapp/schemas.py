from datetime import datetime

from pydantic import AliasPath, ConfigDict, Field

from druks.schemas import Schema


class SessionResponse(Schema):
    model_config = ConfigDict(from_attributes=True)

    id: str
    number: str | None = Field(None, validation_alias=AliasPath("identity", "number"))
    name: str | None = Field(None, validation_alias=AliasPath("identity", "name"))
    admin: str | None = Field(None, validation_alias=AliasPath("identity", "admin", "name"))
    identity_status: str | None
    revoked_at: datetime | None
    revoked_reason: str


class QrResponse(Schema):
    mimetype: str
    data: str

from datetime import datetime

from pydantic import AliasPath, ConfigDict, Field

from druks.schemas import Schema


class NumberResponse(Schema):
    model_config = ConfigDict(from_attributes=True)

    id: str
    number: str = Field(validation_alias=AliasPath("identity", "number"))
    revoked_at: datetime | None


class TwilioNumberResponse(Schema):
    sid: str
    number: str = Field(validation_alias="phone_number")

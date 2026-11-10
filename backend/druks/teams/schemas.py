from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from druks.accounts.schemas import AccountResponse
from druks.schemas import Schema

NonBlank = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class TeamRequest(BaseModel):
    name: NonBlank
    member_ids: list[str] = Field(alias="memberIds")
    apps: list[str]


class TeamResponse(Schema):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    members: list[AccountResponse]
    apps: list[str]

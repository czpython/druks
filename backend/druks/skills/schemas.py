from datetime import datetime

from pydantic import ConfigDict

from druks.schemas import Schema


class SkillResponse(Schema):
    model_config = ConfigDict(from_attributes=True)

    name: str
    description: str
    enabled: bool
    updated_at: datetime


class CollectionResponse(Schema):
    model_config = ConfigDict(from_attributes=True)

    id: str
    source: str
    name: str
    updated_at: datetime
    skills: list[SkillResponse]

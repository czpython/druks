from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from druks.schemas import Schema

NonBlank = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class AgentRequest(BaseModel):
    name: NonBlank
    description: str
    instructions: str
    skills: list[str]
    harness: str
    model: str
    is_private: bool = Field(alias="isPrivate")


class AgentUpdateRequest(AgentRequest):
    owner_id: str = Field(alias="ownerId")


class AgentResponse(Schema):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    description: str
    instructions: str
    skills: list[str]
    harness: str
    model: str
    owner_id: str
    is_private: bool

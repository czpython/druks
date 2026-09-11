from datetime import datetime

from pydantic import AliasPath, BaseModel, ConfigDict, Field

from druks.contrib.software_factory.issues.enums import Priority, Status
from druks.schemas import Schema


class CommentRead(Schema):
    model_config = ConfigDict(from_attributes=True)

    id: int
    author: str = Field(validation_alias=AliasPath("author", "username"))
    body: str
    created_at: datetime


class TicketDetail(Schema):
    """The ticket and its thread, oldest comment first."""

    model_config = ConfigDict(from_attributes=True)

    identifier: str
    title: str
    description: str
    status: Status
    priority: Priority
    repo_id: int
    owner_id: str | None
    comments: list[CommentRead]


class TicketEdit(BaseModel):
    """A partial edit: an omitted field keeps its value. Status moves through ``set_status``."""

    title: str | None = None
    description: str | None = None
    priority: Priority | None = None
    owner_id: str | None = None
    repo_id: int | None = None

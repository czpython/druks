from datetime import datetime

from druks.schemas import Schema
from pydantic import ConfigDict


class NoteSummary(Schema):
    model_config = ConfigDict(from_attributes=True)

    id: int
    body: str
    gist: str | None = None
    created_at: datetime

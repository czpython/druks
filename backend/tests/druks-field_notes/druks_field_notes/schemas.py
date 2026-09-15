from datetime import datetime

from druks.workflows import SubjectSummary
from pydantic import Field


class NoteSummary(SubjectSummary):
    # The note's domain header — what only field_notes knows. The platform's subject
    # read-side composes it with the generic status + timeline.
    body: str
    title: str = Field(validation_alias="body")
    gist: str | None = None
    created_at: datetime


class RepositorySummary(SubjectSummary):
    repo: str
    gist: str | None = None

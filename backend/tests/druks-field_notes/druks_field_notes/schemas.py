from datetime import datetime

from druks.workflows import SubjectSummary


class NoteSummary(SubjectSummary):
    # The note's domain header — what only field_notes knows. The platform's subject
    # read-side composes it with the generic status + timeline.
    body: str
    summary: str | None = None
    created_at: datetime

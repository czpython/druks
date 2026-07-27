from datetime import datetime
from typing import TYPE_CHECKING

from druks.workflows import SubjectSummary

if TYPE_CHECKING:
    from druks_field_notes.models import Note


class NoteSummary(SubjectSummary):
    # The note's domain header — what only field_notes knows. The platform's subject
    # read-side composes it with the generic status + timeline.
    body: str
    summary: str | None = None
    created_at: datetime

    @classmethod
    def from_note(cls, note: "Note") -> "NoteSummary":
        return cls(
            id=str(note.id),
            label=note.label,
            body=note.body,
            summary=note.summary,
            created_at=note.created_at,
        )

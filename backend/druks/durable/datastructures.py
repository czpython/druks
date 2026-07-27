from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True, kw_only=True)
class Subject:
    """What a run is about, as identity alone — a pull request, an issue, a
    conversation. ``Subject(id="owner/repo#7", subject_type="pull_request")`` is
    all a run, an event, or a read ever needs, and it shows itself as that id.
    An extension that also keeps a row of its own subclasses ``StoredSubject``
    instead."""

    id: str
    subject_type: str

    @property
    def identity(self) -> dict[str, Any]:
        return {"type": self.subject_type, "id": self.id}

    @property
    def label(self) -> str:
        # An identity-only subject is already named by its id — "owner/repo#7" is
        # the handle, not a surrogate key.
        return self.id

    @classmethod
    def list_open(cls, subject_type: str, *, limit: int = 50) -> list["Subject"]:
        """The subjects of this type whose newest run hasn't handed off — still going,
        or failed and wanting the operator. What an extension's active view lists when
        the subject is identity alone; a subject with rows of its own lists them with
        ``StoredSubject.list_open``."""
        # Cycle: the durable read side is built on this package's models.
        from druks.database import db_session
        from druks.durable.models import Run

        open_ids = db_session().scalars(Run.open_subject_ids(subject_type).limit(limit))
        return [cls(id=subject_id, subject_type=subject_type) for subject_id in open_ids]

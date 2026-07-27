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

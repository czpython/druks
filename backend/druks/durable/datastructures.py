from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class Subject:
    """What a run is about, as identity alone — a pull request, an issue, a
    conversation. ``Subject("owner/repo#7", "pull_request")`` is all a run, an
    event, or a read ever needs; an extension that also keeps a row of its own
    subclasses ``StoredSubject`` instead."""

    id: str
    subject_type: str

    @property
    def identity(self) -> dict[str, Any]:
        return {"type": self.subject_type, "id": self.id}

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from druks.durable.schemas import RunResponse, SubjectStatus
    from druks.workflows import Workflow


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

    def get_status(self, *, workflow: "type[Workflow] | None" = None) -> "SubjectStatus":
        """Where this subject stands: the state of the run driving it, narrowed to one
        workflow's runs when a subject has several kinds in flight."""
        from druks.durable.reads import get_subject_status

        return get_subject_status(self.subject_type, self.id, workflow=workflow)

    def get_timeline(self) -> "list[RunResponse]":
        """Every run about this subject, oldest first, each with its agent calls."""
        from druks.durable.reads import list_subject_timeline

        return list_subject_timeline(self.subject_type, self.id)

    async def get_phase(self) -> str | None:
        """The step it is on right now ("provisioning_vm"), while something is running."""
        from druks.durable.reads import get_subject_phase

        return await get_subject_phase(self.subject_type, self.id)

    @classmethod
    def list_open(cls, subject_type: str, *, limit: int = 50) -> list["Subject"]:
        """The subjects of this type whose newest run hasn't handed off — still going,
        or failed and wanting the operator. What an extension's active view lists when
        the subject is identity alone; a subject with rows of its own lists them with
        ``StoredSubject.list_open``."""
        # Cycle: the durable read side is built on this package's models.
        from druks.durable.enums import OPEN_STATES
        from druks.durable.models import Run

        states = Run.subject_states(subject_type, limit=limit)
        return [
            cls(id=subject_id, subject_type=subject_type)
            for subject_id, state in states.items()
            if state in OPEN_STATES
        ][:limit]

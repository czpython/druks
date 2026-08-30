from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar, Self

from druks.models import snake_name

if TYPE_CHECKING:
    from druks.durable.schemas import SubjectStatus, SubjectSummary
    from druks.workflows import Workflow


@dataclass(frozen=True, slots=True, kw_only=True)
class Subject:
    """What a run is about, as identity alone — a pull request, a conversation.
    Subclass it: the class name is the subject type, so ``PullRequest`` is
    ``pull_request``, and the id is the whole record — ``owner/repo#7`` is what a
    run, an event, or a read is keyed by, and what the subject shows itself as.
    An app that also keeps a row of its own subclasses ``StoredSubject``
    instead."""

    subject_type: ClassVar[str]

    id: str

    def __init_subclass__(cls, **kwargs: Any) -> None:
        # Explicit args: ``slots=True`` rebuilds this class, leaving a zero-arg
        # ``super()`` bound to the one it replaced.
        super(Subject, cls).__init_subclass__(**kwargs)
        cls.subject_type = snake_name(cls.__name__)

    @property
    def identity(self) -> dict[str, Any]:
        return {"type": self.subject_type, "id": self.id}

    @property
    def label(self) -> str:
        # An identity-only subject is already named by its id — "owner/repo#7" is
        # the handle, not a surrogate key.
        return self.id

    @classmethod
    async def get_for_subject_id(cls, subject_id: str) -> Self | None:
        """The subject this id names. Ids reach the read side as free text off a URL,
        so override to return None for a shape this subject could never wear."""
        return cls(id=subject_id)

    def get_summary(self) -> "SubjectSummary":
        """The header its board and page show it under — the app's own fields;
        the read side composes it with the platform's status and timeline."""
        raise NotImplementedError(
            f"a workflow declares {type(self).__name__}, so it needs a get_summary()"
        )

    @classmethod
    async def list_summaries(cls, account_id: str | None) -> "Sequence[SubjectSummary]":
        """The subjects on this class's board, newest-movement first, each as its domain
        summary. ``account_id`` is the caller, or None outside a request. A shared
        board ignores it. Returns a covariant ``Sequence`` so an app can return
        a ``list`` of its own ``SubjectSummary`` subclass. Required once a workflow
        declares it."""
        raise NotImplementedError(
            f"a workflow declares {cls.__name__}, so it needs a list_summaries()"
        )

    async def get_status(self, *, workflow: "type[Workflow] | None" = None) -> "SubjectStatus":
        """Where this subject stands: the state of the run driving it, narrowed to one
        workflow's runs when a subject has several kinds in flight."""
        from druks.durable.reads import get_subject_status

        return await get_subject_status(self.subject_type, self.id, workflow=workflow)

    async def get_phase(self) -> str | None:
        """The step it is on right now ("provisioning_vm"), while something is running."""
        from druks.durable.reads import get_subject_phase

        return await get_subject_phase(self.subject_type, self.id)

    @classmethod
    async def list_open(cls, *, limit: int = 50) -> list[Self]:
        """The subjects of this class whose newest run hasn't handed off — still going,
        or failed and wanting the operator. What an app's active view lists when
        the subject is identity alone; a subject with rows of its own lists them with
        ``StoredSubject.list_open``."""
        # Cycle: the durable read side is built on this package's models.
        from druks.database import db_session
        from druks.durable.models import Run

        open_ids = await db_session().scalars(Run.open_subject_ids(cls.subject_type).limit(limit))
        return [cls(id=subject_id) for subject_id in open_ids]

from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar, Self

from druks.apps.loader import resolve_workflow_app
from druks.apps.registry import workflows as workflow_registry
from druks.db import atomic, db_session
from druks.durable.schemas import SubjectSummary
from druks.events.models import Event
from druks.exceptions import ObjectNotFound
from druks.models import snake_name

if TYPE_CHECKING:
    from druks.durable.schemas import SubjectStatus
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

    def __str__(self) -> str:
        """How the subject shows itself. An identity-only subject is already named by
        its id: "owner/repo#7" is the handle, not a surrogate key."""
        return self.id

    @property
    def key(self) -> str:
        return str(self)

    async def announce(self, topic: str, **facts: Any) -> None:
        """Record and deliver a domain fact. The event and every subscriber's writes
        commit together, or none does."""
        async with atomic():
            await Event.announce(db_session(), self, topic, facts)

    @classmethod
    async def get_or_none(cls, id: str) -> Self | None:
        """The subject this id names. Ids reach the read side as free text off a URL,
        so override to return None for a shape this subject could never wear."""
        return cls(id=id)

    @classmethod
    async def get(cls, id: str) -> Self:
        """The subject this id names. A shape it could never wear raises
        ``ObjectNotFound``, which a route answers with 404 and a page with an empty
        state."""
        if subject := await cls.get_or_none(id):
            return subject
        raise ObjectNotFound(cls.subject_type.replace("_", " "), {"id": id})

    def get_summary(self) -> SubjectSummary:
        """The header the platform's own screens show. An app with its own frontend
        overrides it to add the fields that frontend reads."""
        return SubjectSummary.model_validate(self)

    @classmethod
    async def list_summaries(cls, account_id: str | None) -> Sequence[SubjectSummary]:
        """The subjects on this class's board, newest movement first, each as its
        summary. ``account_id`` is the caller, or None outside a request. A shared
        board ignores it. Returns a covariant ``Sequence`` so an app can return
        a ``list`` of its own ``SubjectSummary`` subclass. Required once a workflow
        declares it."""
        raise NotImplementedError(
            f"a workflow declares {cls.__name__}, so it needs a list_summaries()"
        )

    @classmethod
    def _list_app_workflows(cls) -> "list[type[Workflow]]":
        """The workflows of the app this class belongs to. Its reads count their runs,
        so another app's run on the same subject does not show."""
        app_name = resolve_workflow_app(cls.__module__)
        return [workflow for workflow in workflow_registry.all() if workflow.app == app_name]

    async def get_status(self, *, workflow: "type[Workflow] | None" = None) -> "SubjectStatus":
        """Where this subject stands: the state of the newest run of its app's workflows,
        or of ``workflow`` alone."""
        from druks.durable.reads import get_subject_status

        if workflow:
            workflows = [workflow]
        else:
            workflows = self._list_app_workflows()
        return await get_subject_status(
            db_session(), self.subject_type, self.id, workflows=workflows
        )

    @classmethod
    async def get_statuses(cls, subject_ids: Sequence[str]) -> "dict[str, SubjectStatus]":
        """Where a whole board stands, keyed by subject id — one read for the whole
        board, so a page listing subjects does not ask once per row."""
        from druks.durable.reads import get_subject_statuses

        return await get_subject_statuses(
            db_session(),
            cls.subject_type,
            list(subject_ids),
            workflows=cls._list_app_workflows(),
        )

    async def get_phase(self) -> str | None:
        """The step it is on right now ("provisioning_vm"), while something is running."""
        from druks.durable.reads import get_subject_phase

        return await get_subject_phase(
            db_session(), self.subject_type, self.id, workflows=self._list_app_workflows()
        )

    @classmethod
    async def list_open(
        cls, *, limit: int = 50, workflows: "Sequence[type[Workflow]] | None" = None
    ) -> list[Self]:
        """The subjects of this class whose newest run hasn't handed off — still going,
        or failed and wanting the operator. It counts the runs of its app's workflows,
        or of ``workflows`` when given. What an app's active view lists when the subject
        is identity alone; a subject with rows of its own lists them with
        ``StoredSubject.list_open``."""
        # Cycle: the durable read side is built on this package's models.
        from druks.db import db_session
        from druks.durable.models import Run

        # An empty list counts no runs, so only None means the class's own app.
        if workflows is None:  # ast-grep-ignore: rule-2
            workflows = cls._list_app_workflows()
        kinds = [workflow.kind for workflow in workflows]
        open_ids = await db_session().scalars(
            Run.open_subject_ids(cls.subject_type, kinds=kinds).limit(limit)
        )
        return [cls(id=subject_id) for subject_id in open_ids]

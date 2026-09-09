from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import Index, Select, and_, or_, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from druks.apps.loader import iter_apps, resolve_workflow_app
from druks.database import db_session
from druks.models import Base, StoredSubject
from druks.signals import publish

if TYPE_CHECKING:
    from druks.durable.datastructures import Subject


class Event(Base):
    """Recorded workflow and domain facts, keyed to their subject."""

    __tablename__ = "events"
    # Newest-per-subject is the history/dashboard rollup; the feed orders on the
    # monotonic pk.
    __table_args__ = (Index("events_subject_idx", "subject_type", "subject_id", "created_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    type: Mapped[str]
    subject_id: Mapped[str | None] = mapped_column(default=None)
    # What the event is about (a work item, a signal), supplied by the caller.
    # Opaque here: the log keys events without knowing the subject.
    subject_type: Mapped[str | None] = mapped_column(default=None)
    # How the subject showed itself when the event was written — the feed labels
    # rows without ever loading a subject. Absent exactly when the subject is.
    subject_label: Mapped[str | None] = mapped_column(default=None)
    app: Mapped[str | None] = mapped_column(default=None)
    # Append-only, so creation time is the event time. No updated_at.
    created_at: Mapped[datetime] = mapped_column(default=Base.utc_now)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)

    @classmethod
    def get_history(
        cls,
        *,
        app: str | None = None,
        search: str | None = None,
        kind: str | None = None,
        from_at: datetime | None = None,
        until: datetime | None = None,
    ) -> Select[tuple["Event"]]:
        """The recorded Activity that matches these filters, as a query. Search reads the
        recorded subject label literally; from is inclusive and until is exclusive."""
        # The durable package imports this module.
        from druks.durable.enums import WorkflowEvent

        owners = [owner.name for owner in iter_apps() if not owner.builtin]
        statement = select(cls).where(
            cls.app.in_(owners),
            or_(
                cls.type.not_like("workflow.%"),
                cls.type.in_(
                    [
                        WorkflowEvent.SCHEDULED,
                        WorkflowEvent.PARKED,
                        WorkflowEvent.FAILED,
                        WorkflowEvent.CANCELLED,
                    ]
                ),
                # A validated gate reply records its result; a routine start records none.
                and_(cls.type == WorkflowEvent.RUNNING, cls.payload.has_key("result")),
            ),
        )
        if app:
            statement = statement.where(cls.app == app)
        if search and search.strip():
            statement = statement.where(
                cls.subject_label.icontains(search.strip(), autoescape=True)
            )
        if kind:
            statement = statement.where(cls.type == kind)
        if from_at:
            statement = statement.where(cls.created_at >= from_at)
        if until:
            statement = statement.where(cls.created_at < until)
        return statement

    @classmethod
    async def emit(
        cls,
        *,
        type: str,
        subject: dict[str, Any] | None = None,
        label: str | None = None,
        payload: dict[str, Any] | None = None,
        app: str | None = None,
        session: AsyncSession | None = None,
    ) -> None:
        """Record in the supplied session or the current domain transaction."""
        session = session or db_session()
        subject = subject or {}
        session.add(
            cls(
                type=type,
                subject_type=subject.get("type"),
                subject_id=str(subject["id"]) if "id" in subject else None,
                subject_label=label or None,
                app=app,
                payload=payload or {},
            )
        )
        await session.flush()

    @classmethod
    async def announce(
        cls, subject: "Subject | StoredSubject", topic: str, facts: dict[str, Any]
    ) -> None:
        """Record a subject's domain fact and notify subscribers, in the current
        transaction. A failing subscriber rolls the domain change back with it."""
        # The durable package imports this module.
        from druks.durable.exceptions import WorkflowError

        try:
            app = resolve_workflow_app(type(subject).__module__)
        except LookupError:
            raise WorkflowError(
                f"{type(subject).__module__} declares subject {type(subject).__name__} outside "
                "every registered app package. Call register_workflow_package() for the "
                "package before importing it."
            ) from None
        await cls.emit(
            type=topic, subject=subject.identity, label=subject.label, payload=facts, app=app
        )
        await publish(topic, subject=subject.identity, **facts)

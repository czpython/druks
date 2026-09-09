from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import Index
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from druks.database import db_session
from druks.models import Base, StoredSubject

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
        # The apps package, the signals bus, and the durable engine are built on this log.
        from druks.apps.loader import resolve_workflow_app
        from druks.durable.exceptions import WorkflowError
        from druks.signals import publish

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

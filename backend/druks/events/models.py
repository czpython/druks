from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import ColumnElement, Index, Select, Text, and_, cast, func, not_, or_, select, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import UserDefinedType

from druks.apps.loader import iter_apps, resolve_workflow_app
from druks.models import Base, StoredSubject
from druks.signals import publish

if TYPE_CHECKING:
    from druks.durable.datastructures import Subject


class TransactionId(UserDefinedType[int]):
    cache_ok = True

    def get_col_spec(self, **kwargs: Any) -> str:
        return "xid8"


class Snapshot(UserDefinedType[str]):
    cache_ok = True

    def get_col_spec(self, **kwargs: Any) -> str:
        return "pg_snapshot"


class Event(Base):
    """Recorded workflow and domain facts, keyed to their subject."""

    __tablename__ = "events"
    # Newest-per-subject is the history/dashboard rollup; the feed orders on the
    # monotonic pk.
    __table_args__ = (
        Index("events_subject_idx", "subject_type", "subject_id", "created_at"),
        Index("events_xid_idx", "xid"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    xid: Mapped[int] = mapped_column(TransactionId(), server_default=text("pg_current_xact_id()"))
    type: Mapped[str]
    subject_id: Mapped[str | None] = mapped_column(default=None)
    # What the event is about (a work item, a signal), supplied by the caller.
    # Opaque here: the log keys events without knowing the subject.
    subject_type: Mapped[str | None] = mapped_column(default=None)
    # How the subject showed itself when the event was written — the feed labels
    # rows without ever loading a subject. Absent exactly when the subject is.
    subject_key: Mapped[str | None] = mapped_column(default=None)
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
        topic: str | None = None,
        from_at: datetime | None = None,
        until: datetime | None = None,
    ) -> Select[tuple["Event"]]:
        """The recorded Activity that matches these filters, as a query. Search reads the
        recorded key and title literally; from is inclusive and until is exclusive."""
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
                or_(
                    cls.subject_key.icontains(search.strip(), autoescape=True),
                    cls.payload["title"].as_string().icontains(search.strip(), autoescape=True),
                )
            )
        if topic:
            statement = statement.where(cls.type == topic)
        if from_at:
            statement = statement.where(cls.created_at >= from_at)
        if until:
            statement = statement.where(cls.created_at < until)
        return statement

    @classmethod
    async def get_cursor(cls, session: AsyncSession, cursor: str | None = None) -> str:
        """The snapshot a live stream continues from: ``cursor`` once Postgres accepts
        it, or the current snapshot."""
        return await session.scalar(
            select(func.coalesce(cast(cursor, Snapshot()), func.pg_current_snapshot()).cast(Text))
        )

    @classmethod
    def committed_after(cls, cursor: str) -> ColumnElement[bool]:
        """The rows the snapshot ``cursor`` did not see, including the ones whose
        transaction was still running when it was taken."""
        snapshot = cast(cursor, Snapshot())
        # Everything below xmin is visible, so the index range starts there.
        return and_(
            cls.xid >= func.pg_snapshot_xmin(snapshot),
            not_(func.pg_visible_in_snapshot(cls.xid, snapshot)),
        )

    @classmethod
    async def emit(
        cls,
        session: AsyncSession,
        *,
        type: str,
        subject: dict[str, Any] | None = None,
        key: str | None = None,
        title: str | None = None,
        run: str | None = None,
        kind: str | None = None,
        facts: dict[str, Any] | None = None,
        app: str | None = None,
    ) -> None:
        """Record the work identity, the run, and the announced facts in this transaction."""
        # The durable package imports this module.
        from druks.durable.exceptions import WorkflowError

        subject = subject or {}
        facts = facts or {}
        recorded = {"run": run, "kind": kind, "title": title}
        if taken := recorded.keys() & facts.keys():
            raise WorkflowError(f"{type} facts {sorted(taken)} belong to Druks. Rename them.")
        session.add(
            cls(
                type=type,
                subject_type=subject.get("type"),
                subject_id=str(subject["id"]) if "id" in subject else None,
                subject_key=key or None,
                app=app,
                payload={**facts, **{name: value for name, value in recorded.items() if value}},
            )
        )
        await session.flush()

    @classmethod
    async def announce(
        cls,
        session: AsyncSession,
        subject: "Subject | StoredSubject",
        topic: str,
        facts: dict[str, Any],
    ) -> None:
        """Record a subject's domain fact and notify subscribers, in the session's
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
            session,
            type=topic,
            subject=subject.identity,
            key=subject.key,
            title=subject.get_summary().title,
            facts=facts,
            app=app,
        )
        await publish(topic, subject=subject.identity, **facts)

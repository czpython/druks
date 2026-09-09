from datetime import datetime
from typing import Any

from sqlalchemy import Index
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from druks.database import db_session
from druks.models import Base


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

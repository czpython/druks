from datetime import datetime

from druks.db import StoredSubject, db_session
from sqlalchemy import select
from sqlalchemy.orm import Mapped, mapped_column


class Note(StoredSubject):
    __tablename__ = "field_notes_notes"

    # What the note is about — the raw observation an operator jotted down. A run's
    # agent reads this and writes back a one-line summary.
    body: Mapped[str]
    # The agent's summary of ``body``, written when a Summarize run finishes. None
    # until then.
    summary: Mapped[str | None]
    created_at: Mapped[datetime] = mapped_column(default=StoredSubject.utc_now)

    @classmethod
    def create(cls, *, body: str) -> "Note":
        session = db_session()
        note = cls(body=body)
        session.add(note)
        session.flush()
        return note

    @classmethod
    def get(cls, note_id: int) -> "Note | None":
        return db_session().get(cls, note_id)

    @classmethod
    def list_recent(cls, *, limit: int = 100) -> list["Note"]:
        stmt = select(cls).order_by(cls.created_at.desc(), cls.id.desc()).limit(limit)
        return list(db_session().scalars(stmt))

    def save_summary(self, summary: str) -> None:
        self.summary = summary
        db_session().flush()

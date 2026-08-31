from datetime import datetime

from druks.db import StoredSubject, db_session
from sqlalchemy import select
from sqlalchemy.orm import Mapped, mapped_column

from druks_field_notes.schemas import NoteSummary


class Note(StoredSubject):
    __tablename__ = "field_notes_notes"

    # What the note is about — the raw observation an operator jotted down. A run's
    # agent reads this and writes back its gist.
    body: Mapped[str]
    # ``body`` in one line, written when a Summarize run finishes. None until then.
    gist: Mapped[str | None]
    created_at: Mapped[datetime] = mapped_column(default=StoredSubject.utc_now)

    @classmethod
    async def create(cls, *, body: str) -> "Note":
        session = db_session()
        note = cls(body=body)
        session.add(note)
        await session.flush()
        return note

    @classmethod
    async def get(cls, note_id: int) -> "Note | None":
        return await db_session().get(cls, note_id)

    @classmethod
    async def list_recent(cls, *, limit: int = 100) -> list["Note"]:
        stmt = select(cls).order_by(cls.created_at.desc(), cls.id.desc()).limit(limit)
        return list(await db_session().scalars(stmt))

    async def save_gist(self, gist: str) -> None:
        self.gist = gist
        await db_session().flush()

    def get_summary(self) -> NoteSummary:
        return NoteSummary.model_validate(self)

    @classmethod
    async def list_summaries(cls, account_id: str | None) -> list[NoteSummary]:
        # How many the board shows is an operator knob, so it lives on the app.
        from druks_field_notes.app import FieldNotes

        notes = await cls.list_recent(limit=(await FieldNotes.settings()).board_size)
        return [note.get_summary() for note in notes]

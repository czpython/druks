from druks.workflows import Workflow

from druks_field_notes.extension import FieldNotes
from druks_field_notes.models import Note


class Summarize(Workflow):
    """Reads one note and writes its summary — a single durable operation: the
    agent produces the summary prose, and the run stores it on the note."""

    async def run(self, note_id: int) -> None:
        note = Note.get(note_id)
        assert note is not None  # dispatched against a note the route just created
        # The note body is the agent's prompt context; the summary it returns is the
        # extension's own domain result, saved onto the note.
        result = await FieldNotes.summarize(note_body=note.body)
        note.save_summary(result.summary)

    @classmethod
    async def dispatch(cls, *, note_id: int, account_id: str | None = None) -> str:
        # Launch policy for a note: one run per note, keyed by its subject,
        # attributed to the account that asked for it.
        return await cls.start(
            subject={"type": FieldNotes.subject_type, "id": note_id},
            account_id=account_id,
            note_id=note_id,
        )

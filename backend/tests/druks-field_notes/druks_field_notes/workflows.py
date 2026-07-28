from druks.workflows import Workflow

from druks_field_notes.extension import FieldNotes
from druks_field_notes.models import Note


class Summarize(Workflow):
    """Reads one note and writes its summary — a single durable operation: the
    agent produces the summary prose, and the run stores it on the note."""

    subject = Note

    async def run(self) -> None:
        note = self.subject
        # The note body is the agent's prompt context; the summary it returns is the
        # extension's own domain result, saved onto the note.
        result = await FieldNotes.summarize(note_body=note.body)
        note.save_summary(result.summary)

    @classmethod
    async def dispatch(cls, *, note: Note) -> str:
        # Launch policy for a note: one run per note, keyed by its subject; the
        # signed-in requester attributes it ambiently.
        return await cls.start(subject=note)

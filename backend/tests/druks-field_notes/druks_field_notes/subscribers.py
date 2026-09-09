from druks.signals import subscribe
from druks.workflows import WorkflowEvent

from druks_field_notes.models import Note
from druks_field_notes.workflows import Summarize


@subscribe(WorkflowEvent.FINISHED, workflow=Summarize)
async def note_summarized(*, subject: Note, **_: object) -> None:
    await subject.announce("note.gist_saved")

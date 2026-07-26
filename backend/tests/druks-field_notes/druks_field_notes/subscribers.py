from druks.signals import subscribe
from druks.workflows import WorkflowEvent

from druks_field_notes.extension import FieldNotes
from druks_field_notes.models import Note
from druks_field_notes.workflows import Summarize


@subscribe(WorkflowEvent.FINISHED, workflow=Summarize, subject=Note)
async def note_summarized(*, subject: Note, **_: object) -> None:
    # A finished summarize is a milestone worth its own feed row — record it so
    # format_event can render it. The workflow lifecycle is the trigger; the
    # extension only reacts.
    FieldNotes.record_event(type="summarized", subject=subject)

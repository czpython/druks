from druks.signals import subscribe

from druks_field_notes.extension import FieldNotes
from druks_field_notes.models import Note
from druks_field_notes.workflows import Summarize


@subscribe("run.finished", kind=Summarize.kind, subject=Note)
async def note_summarized(*, subject: Note, **_: object) -> None:
    # A finished summarize run is a milestone worth its own feed row — record it so
    # format_event can render it. The run lifecycle is the trigger; the extension
    # only reacts.
    FieldNotes.record_event(type="summarized", subject=subject)

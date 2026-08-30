from druks.ui import Page, page


@page("/")
async def notes():
    return Page(title="Notes", description="Every note this install captured.")


@notes.child("/recent")
async def recent_notes():
    return Page(title="Recent notes")


@page("/notes/new")
async def new_note():
    return Page(title="Write a note")


@page("/notes/{note_id}")
async def note(note_id: int):
    return Page(title=f"Note {note_id}")


@note.child("/history")
async def note_history(note_id: int):
    return Page(title=f"Note {note_id} history")

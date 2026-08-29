from druks.ui import (
    Callout,
    Card,
    Divider,
    EmptyState,
    GateControls,
    Link,
    Markdown,
    Page,
    Section,
    Text,
    page,
)

from druks_field_notes.models import Note


@page("/")
async def notes():
    recent = await Note.list_recent(limit=10)
    if recent:
        return Page(
            title="Notes",
            description="Every note this install captured.",
            blocks=[
                Section(
                    title="Recent",
                    name="recent",
                    blocks=[
                        Card(
                            title=f"Note {note.id}",
                            description=note.gist or "Waiting for its gist.",
                            blocks=[Text(note.body)],
                            actions=[
                                Link("Open", page="note", arguments={"note_id": str(note.id)})
                            ],
                        )
                        for note in recent
                    ],
                )
            ],
        )
    return Page(
        title="Notes",
        description="Every note this install captured.",
        blocks=[
            EmptyState(
                "No notes yet",
                description="Write one and its gist appears here.",
                actions=[Link("Write a note", page="new_note")],
            )
        ],
    )


@notes.child("/recent")
async def recent_notes():
    recent = await Note.list_recent(limit=3)
    return Page(
        title="Recent notes",
        blocks=[Text(f"The last {len(recent)} notes this install captured.")],
    )


@page("/notes/new")
async def new_note():
    return Page(
        title="Write a note",
        blocks=[
            Callout("Notes arrive through the API.", tone="info", title="Not here yet"),
            Divider(),
            Markdown("Post a note to `/api/field_notes/notes`."),
        ],
    )


@page("/notes/{note_id}")
async def note(note_id: int):
    found = await Note.get(note_id)
    if found:
        status = await found.get_status()
        # The region follows the note, so answering the gate refreshes it and
        # the controls go away.
        if status.gate:
            decision = [GateControls(status.run)]
        else:
            decision = [Text("Nothing is waiting on you.")]
        return Page(
            title=f"Note {note_id}",
            description=found.gist or "Waiting for its gist.",
            blocks=[
                Markdown(found.body),
                Section(title="Your decision", name="decision", follows=found, blocks=decision),
            ],
        )
    return Page(title=f"Note {note_id}", blocks=[EmptyState("No such note")])


@note.child("/history")
async def note_history(note_id: int):
    return Page(
        title=f"Note {note_id} history",
        blocks=[Text("Every run about this note lands here.")],
    )

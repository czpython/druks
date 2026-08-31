from druks import ui

from druks_field_notes.models import Note


@ui.page("/")
async def notes():
    recent = await Note.list_recent(limit=10)
    return ui.Page(
        "Notes",
        description="Every note this install captured.",
        blocks=[
            ui.Section(
                name="recent",
                blocks=[
                    ui.Cards(
                        title="Recent",
                        cards=[
                            ui.Card(
                                title=f"Note {note.id}",
                                description=note.gist or "Waiting for its gist.",
                                blocks=[ui.Text(note.body)],
                                actions=[
                                    ui.Link(
                                        "Open",
                                        page="note",
                                        arguments={"note_id": str(note.id)},
                                    )
                                ],
                            )
                            for note in recent
                        ],
                        empty=ui.EmptyState(
                            "No notes yet",
                            description="Write one and its gist appears here.",
                            actions=[ui.Link("Write a note", page="new_note")],
                        ),
                    )
                ],
            )
        ],
    )


@notes.child("/recent")
async def recent_notes():
    recent = await Note.list_recent(limit=3)
    summarized = [note for note in recent if note.gist]
    return ui.Page(
        "Recent notes",
        blocks=[
            ui.Stack(
                [
                    ui.Metrics(
                        [
                            ui.Metric("Captured", value=ui.NumberValue(len(recent))),
                            ui.Metric(
                                "Summarized",
                                value=ui.NumberValue(len(summarized)),
                                description="Notes an agent has read.",
                            ),
                        ]
                    ),
                    ui.Columns(
                        [
                            ui.Facts(
                                [
                                    ui.Fact("Body", value=ui.TextValue(note.body)),
                                    ui.Fact("Captured", value=ui.TimeValue(note.created_at)),
                                ],
                                title="Latest",
                            )
                            for note in recent[:1]
                        ]
                    ),
                    ui.Table(
                        title="Notes",
                        columns=[
                            ui.TableColumn("Note"),
                            ui.TableColumn("Gist"),
                            ui.TableColumn("Captured", align="end"),
                        ],
                        rows=[
                            ui.TableRow(
                                [
                                    ui.TextValue(
                                        f"Note {note.id}",
                                        link=ui.Link(
                                            f"Note {note.id}",
                                            page="note",
                                            arguments={"note_id": str(note.id)},
                                        ),
                                    ),
                                    ui.StatusValue(
                                        "summarized" if note.gist else "waiting",
                                        tone="success" if note.gist else "warning",
                                    ),
                                    ui.TimeValue(note.created_at),
                                ]
                            )
                            for note in recent
                        ],
                        empty_text="No notes yet.",
                    ),
                    ui.List([ui.TextValue(note.body) for note in recent], title="Bodies"),
                ]
            )
        ],
    )


@ui.page("/notes/new")
async def new_note():
    return ui.Page(
        "Write a note",
        blocks=[
            ui.Callout("An agent writes its gist.", tone="info", title="One line is enough"),
            ui.Divider(),
            ui.Form(
                title="New note",
                description="What did you see?",
                fields=[
                    ui.TextAreaField(
                        name="body",
                        label="Note",
                        placeholder="Fan noise on rack 3.",
                        is_required=True,
                        rows=3,
                    )
                ],
                action=ui.Action(
                    label="Save",
                    operation="write_note",
                    tone="primary",
                    link=ui.Link("Notes", page="notes"),
                ),
            ),
        ],
    )


@ui.page("/notes/{note_id}")
async def note(note_id: int):
    found = await Note.get(note_id)
    if found:
        status = await found.get_status()
        # The region follows the note, so answering the gate refreshes it and
        # the controls go away.
        if status.gate:
            decision = [ui.GateControls(status.run)]
        else:
            decision = [ui.Text("Nothing is waiting on you.")]
        return ui.Page(
            f"Note {note_id}",
            description=found.gist or "Waiting for its gist.",
            blocks=[
                ui.Markdown(found.body),
                ui.Card(
                    title="Gist",
                    description=found.gist or "Waiting for its gist.",
                    actions=[
                        ui.Action(
                            label="Clear the gist",
                            operation="clear_gist",
                            arguments={"note_id": found.id},
                            tone="danger",
                            confirm="Clear this note's gist? An agent has to read it again.",
                        )
                    ],
                ),
                ui.Section(title="Your decision", name="decision", follows=found, blocks=decision),
            ],
        )
    return ui.Page(f"Note {note_id}", blocks=[ui.EmptyState("No such note")])


@note.child("/history")
async def note_history(note_id: int):
    found = await Note.get(note_id)
    if found:
        return ui.Page(
            f"Note {note_id} history",
            blocks=[
                ui.Timeline(
                    [ui.TimelineItem(when=found.created_at, title="Captured")], title="This note"
                ),
                ui.Progress("Summarized", completed=1 if found.gist else 0, total=1),
                ui.Link("Everything druks did about this note", subject=found),
            ],
        )
    return ui.Page(f"Note {note_id} history", blocks=[ui.EmptyState("No such note")])

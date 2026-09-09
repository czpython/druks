from druks.apps import App
from druks.db import db_session
from druks.events import Event
from druks_field_notes.contracts import GistOutput
from druks_field_notes.models import Note, Repository
from druks_field_notes.subscribers import note_summarized
from sqlalchemy import select


def test_gist_declares_one_useful_result():
    output = GistOutput(gist="The pump ran hot.")
    assert output.get_artifact() == {
        "kind": "markdown",
        "title": "Gist",
        "content": "The pump ran hot.",
    }
    assert output.get_activity() == {"kind": "gist.prepared", "summary": "The pump ran hot."}
    assert not hasattr(App, "record_event")


async def test_saved_note_and_repository_announce_distinct_domain_facts(druks_db):
    db_session.registry.set(druks_db)
    note = await Note.create(body="The pump ran hot.")
    await note.save_gist("The pump ran hot.")
    await note_summarized(subject=note)
    repository = await Repository.create(repo="acme/pumps")
    await repository.announce(
        "repository.reported", summary="The source owner published its report."
    )
    events = list(await druks_db.scalars(select(Event).order_by(Event.id)))
    assert [event.type for event in events] == ["note.gist_saved", "repository.reported"]
    assert {event.app for event in events} == {"field_notes"}
    assert [event.subject_type for event in events] == ["note", "repository"]
    assert all("run" not in event.payload for event in events)

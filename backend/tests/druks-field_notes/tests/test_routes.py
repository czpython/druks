from druks_field_notes.models import Note
from druks_field_notes.workflows import Summarize


async def test_notes_routes_create_and_list_notes(druks_client, monkeypatch):
    # The route dispatches from its own session and that session closes with the
    # request, so read the note's id here while the row is still attached.
    summarized = []

    async def dispatch(*, note):
        summarized.append(note.id)
        return "run-1"

    monkeypatch.setattr(Summarize, "dispatch", staticmethod(dispatch))

    created = await druks_client.post(
        "/api/field_notes/notes",
        json={"body": "the pump ran hot"},
    )

    assert created.status_code == 201
    note = await Note.get(created.json()["id"])
    assert note.body == "the pump ran hot"
    assert summarized == [note.id]

    listed = await druks_client.get("/api/field_notes/notes")

    assert listed.status_code == 200
    assert listed.json()[0]["body"] == "the pump ran hot"
    assert listed.json()[0]["gist"] is None

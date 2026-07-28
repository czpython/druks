from druks_field_notes.models import Note


def test_note_create_list_and_save_gist(druks_db):
    first = Note.create(body="the pump ran hot")
    second = Note.create(body="the pressure held")

    assert Note.list_recent(limit=1) == [second]

    first.save_gist("The pump ran hot.")

    saved = Note.get(first.id)
    assert saved.gist == "The pump ran hot."

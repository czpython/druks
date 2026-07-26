from druks_field_notes.models import Note


def test_note_create_list_and_save_summary(druks_db):
    first = Note.create(body="the pump ran hot")
    second = Note.create(body="the pressure held")

    assert Note.list_recent(limit=1) == [second]

    first.save_summary("The pump ran hot.")

    saved = Note.get(first.id)
    assert saved.summary == "The pump ran hot."

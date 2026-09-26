from druks_field_notes.models import Note, Repository


def test_tables_are_named_for_the_app_and_the_class():
    assert Note.__tablename__ == "field_notes_note"
    assert Repository.__tablename__ == "field_notes_repository"


async def test_note_create_list_and_save_gist(druks_db):
    first = await Note.create(body="the pump ran hot")
    second = await Note.create(body="the pressure held")

    assert await Note.list_recent(limit=1) == [second]

    await first.save_gist("The pump ran hot.")

    saved = await Note.get_for_id(first.id)
    assert saved.gist == "The pump ran hot."

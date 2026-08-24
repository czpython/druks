from druks_field_notes.models import Note


async def test_note_create_list_and_save_gist(druks_db):
    first = await Note.create(body="the pump ran hot")
    second = await Note.create(body="the pressure held")

    assert await Note.list_recent(limit=1) == [second]

    await first.save_gist("The pump ran hot.")

    saved = await Note.get(first.id)
    assert saved.gist == "The pump ran hot."

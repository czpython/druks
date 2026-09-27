import pytest
from druks.exceptions import ObjectNotFound
from druks_field_notes.models import Note, Repository


def test_tables_are_named_for_the_app_and_the_class():
    assert Note.__tablename__ == "field_notes_note"
    assert Repository.__tablename__ == "field_notes_repository"


async def test_note_create_list_and_save_gist(druks_db):
    first = await Note.create(body="the pump ran hot")
    second = await Note.create(body="the pressure held")

    assert await Note.list_recent(limit=1) == [second]

    await first.save_gist("The pump ran hot.")

    assert (await Note.get(id=first.id)).gist == "The pump ran hot."


async def test_rows_read_by_field_in_the_declared_order(druks_db):
    first = await Note.create(body="the pump ran hot")
    second = await Note.create(body="the pump ran hot")

    assert await Note.all() == [second, first]
    assert await Note.filter(body="the pump ran hot") == [second, first]
    with pytest.raises(TypeError, match="Note.all()"):
        await Note.filter()
    assert await Note.get(id=str(second.id)) == second
    assert await Note.get_or_none(id="not an id") is None
    with pytest.raises(ObjectNotFound, match="No note with id 0"):
        await Note.get(id=0)

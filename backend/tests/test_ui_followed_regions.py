import pytest
from druks.ui import Card, GateControls, Link, Page, Section, Text
from druks_field_notes.models import Note


@pytest.fixture
async def note(druks_db) -> Note:
    return await Note.create(body="Fan noise on rack 3.")


def test_a_region_follows_the_subject_it_watches(note: Note):
    section = Section(name="decision", follows=note, blocks=[])

    assert section.follows
    assert section.follows.subject_type == "note"
    # A subject id reaches the stream through a URL, so it travels as text.
    assert section.follows.subject_id == str(note.id)


def test_a_page_follows_a_subject_too(note: Note):
    page = Page(title="Note", follows=note)

    assert page.follows
    assert page.follows.subject_id == str(note.id)


def test_a_region_follows_every_subject_of_a_type():
    section = Section(name="board", follows=Note, blocks=[])

    assert section.follows
    assert section.follows.subject_type == "note"
    assert section.follows.subject_id == ""


def test_a_page_follows_every_subject_of_a_type():
    page = Page(title="Notes", follows=Note)

    assert page.follows
    assert page.follows.subject_type == "note"
    assert page.follows.subject_id == ""


def test_a_followed_region_needs_a_name(note: Note):
    with pytest.raises(ValueError, match="needs a name"):
        Section(follows=note, blocks=[])


def test_gate_controls_need_something_that_follows(note: Note):
    with pytest.raises(ValueError, match="follows a subject"):
        Page(title="Note", blocks=[GateControls("run-6f0a")])


def test_a_following_page_is_enough_for_gate_controls(note: Note):
    page = Page(title="Note", follows=note, blocks=[GateControls("run-6f0a")])

    assert page.blocks[0].run == "run-6f0a"


def test_a_following_region_covers_the_blocks_under_it(note: Note):
    page = Page(
        title="Note",
        blocks=[
            Section(
                name="decision",
                follows=note,
                blocks=[Card(blocks=[GateControls("run-6f0a")])],
            )
        ],
    )

    assert page.blocks[0].blocks[0].blocks[0].run == "run-6f0a"


def test_a_region_that_follows_nothing_does_not_cover_gate_controls(note: Note):
    with pytest.raises(ValueError, match="follows a subject"):
        Page(
            title="Note",
            blocks=[Section(name="decision", blocks=[GateControls("run-6f0a")])],
        )


def test_a_page_that_follows_nothing_serializes_it_as_nothing():
    page = Page(title="Note", blocks=[Text("static")])

    assert page.model_dump(by_alias=True, mode="json")["follows"] is None


def test_two_regions_cannot_share_a_name(note: Note):
    with pytest.raises(ValueError, match="two regions named 'decision'"):
        Page(
            title="Note",
            blocks=[
                Section(name="decision", follows=note, blocks=[]),
                Section(name="decision", follows=note, blocks=[]),
            ],
        )


def test_a_nested_region_cannot_take_a_name_already_used(note: Note):
    with pytest.raises(ValueError, match="two regions named 'decision'"):
        Page(
            title="Note",
            blocks=[
                Section(name="decision", follows=note, blocks=[]),
                Card(blocks=[Section(name="decision", follows=note, blocks=[])]),
            ],
        )


def test_follows_takes_a_subject_and_says_so_when_it_does_not(note: Note):
    with pytest.raises(ValueError, match="takes the subject a page watches"):
        Page(title="Note", follows="run-6f0a")


def test_a_link_reaches_the_subjects_own_page(note: Note):
    link = Link("Everything druks did", subject=note)

    assert link.subject
    assert link.subject.subject_type == "note"
    assert link.subject.subject_id == str(note.id)


def test_a_link_takes_exactly_one_destination(note: Note):
    with pytest.raises(ValueError, match="exactly one"):
        Link("Everything druks did", page="notes", subject=note)


def test_a_link_refuses_a_subject_type():
    with pytest.raises(ValueError, match="opens one subject's page"):
        Link("Everything druks did", subject=Note)

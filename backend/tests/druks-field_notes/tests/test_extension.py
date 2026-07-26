import pytest
from druks.events import Event
from druks_field_notes.extension import FieldNotes
from druks_field_notes.models import Note
from pydantic import ValidationError


def test_format_event_describes_the_note():
    event = Event(
        id=1,
        type="workflow.finished",
        subject_type="note",
        subject_id="7",
        extension="field_notes",
        payload={},
        created_at=Event.utc_now(),
    )

    item = FieldNotes.format_event(event)

    assert item.source == "field_notes"
    assert item.summary == "note 7 summarized"
    assert item.link_path == "/app/field_notes/notes/7"


def test_list_subjects_honors_the_board_size(druks_db):
    Note.create(body="first")
    newest = Note.create(body="second")
    FieldNotes.override_setting("board_size", 1)

    subjects = FieldNotes.list_subjects()

    assert [subject.id for subject in subjects] == [str(newest.id)]
    assert subjects[0].body == "second"


def test_settings_validate_the_sync_token():
    settings = FieldNotes.Settings(sync_token="sk-sync-token")

    assert settings.board_size == 50
    assert settings.visibility == "private"
    assert settings.sync_token.get_secret_value() == "sk-sync-token"

    with pytest.raises(ValidationError, match="must start with 'sk-'"):
        FieldNotes.Settings(sync_token="malformed-token")

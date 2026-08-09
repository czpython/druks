import pytest
from druks_field_notes.extension import FieldNotes
from druks_field_notes.models import Note
from pydantic import ValidationError


def test_the_board_honors_the_board_size(druks_db):
    Note.create(body="first")
    newest = Note.create(body="second")
    FieldNotes.override_setting("board_size", 1)

    summaries = Note.list_summaries()

    assert [summary.id for summary in summaries] == [str(newest.id)]
    assert summaries[0].body == "second"


def test_settings_validate_the_sync_token():
    settings = FieldNotes.Settings(sync_token="sk-sync-token")

    assert settings.board_size == 50
    assert settings.visibility == "private"
    assert settings.sync_token.get_secret_value() == "sk-sync-token"

    with pytest.raises(ValidationError, match="must start with 'sk-'"):
        FieldNotes.Settings(sync_token="malformed-token")


def test_settings_require_a_sync_token_for_public_visibility():
    assert FieldNotes.Settings(visibility="public").clean() == {
        "sync_token": "Required when visibility is public."
    }
    assert FieldNotes.Settings(visibility="public", sync_token="sk-sync-token").clean() == {}


def test_the_signing_key_declares_the_multiline_secret_presentation():
    # An author marks a pasted PEM-shaped secret multiline; the platform keeps
    # newlines intact end to end, so the stored value is exactly the paste.
    from druks.extensions.settings import field_kind, field_multiline

    field = FieldNotes.Settings.model_fields["sync_signing_key"]
    assert field_kind(field) == "secret"
    assert field_multiline(field)
    assert not field_multiline(FieldNotes.Settings.model_fields["sync_token"])

    pem = "-----BEGIN KEY-----\nline-one\nline-two\n-----END KEY-----"
    settings = FieldNotes.Settings(sync_signing_key=pem)
    assert settings.sync_signing_key.get_secret_value() == pem

import pytest
from druks.apps.settings import field_kind, field_multiline
from druks_field_notes.app import FieldNotes
from druks_field_notes.models import Note, Repository
from pydantic import ValidationError


async def test_the_board_honors_the_board_size(druks_db):
    await Note.create(body="first")
    newest = await Note.create(body="second")
    await FieldNotes.override_setting("board_size", 1)

    summaries = await Note.list_summaries(None)

    assert [summary.id for summary in summaries] == [str(newest.id)]
    assert summaries[0].body == "second"
    assert summaries[0].title == "second"
    repository = await Repository.create(repo="acme/observations")
    assert repository.get_summary().title is None


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
    # The platform keeps the newlines of a pasted PEM key, so the stored value is the paste.
    field = FieldNotes.Settings.model_fields["sync_signing_key"]
    assert field_kind(field) == "secret"
    assert field_multiline(field)
    assert not field_multiline(FieldNotes.Settings.model_fields["sync_token"])

    pem = "-----BEGIN KEY-----\nline-one\nline-two\n-----END KEY-----"
    settings = FieldNotes.Settings(sync_signing_key=pem)
    assert settings.sync_signing_key.get_secret_value() == pem

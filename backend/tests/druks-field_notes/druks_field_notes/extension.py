import os
from typing import TYPE_CHECKING, Literal

from druks.agents import Agent
from druks.doctor import CheckResult
from druks.extensions import Extension
from druks.workflows import Subject
from pydantic import BaseModel, Field, SecretStr, field_validator

from druks_field_notes.contracts import NoteSummary
from druks_field_notes.models import Note
from druks_field_notes.schemas import NoteView

if TYPE_CHECKING:
    from druks.settings import Settings

# The env var field_notes would read its summarizer credential from. Unset in a
# bare install, so the check below reports it missing — the "extension owns a check
# for its own API key" case, kept to an env read so the proof package needs no real
# provider.
API_KEY_ENV = "FIELD_NOTES_API_KEY"


def check_summary_api_key(settings: "Settings") -> CheckResult:
    """The summarizer needs its provider credential; report a missing one as a
    failure the operator can act on rather than letting the first run blow up."""
    if not os.environ.get(API_KEY_ENV):
        return CheckResult(
            name="summary_api_key",
            ok=False,
            detail=f"{API_KEY_ENV} is unset — the summarize agent can't authenticate.",
        )
    return CheckResult(name="summary_api_key", ok=True, detail="set")


class FieldNotes(Extension):
    name = "field_notes"
    subject_type = Note.subject_type
    icon = "notebook"
    description = "Turns a jotted observation into a one-line summary with an agent."

    class Settings(BaseModel):
        # How many recent notes the board shows — an operator knob, so it lives here.
        board_size: int = Field(
            default=50,
            ge=1,
            le=500,
            title="Board size",
            description="Most-recent notes shown on the field-notes board.",
        )
        # A closed choice set: which notes the board surfaces. A Literal, so the API
        # exposes the options and the settings UI renders a select.
        visibility: Literal["private", "team", "public"] = Field(
            default="private",
            title="Visibility",
            description="Who the field-notes board is shared with.",
        )
        # A secret: the key the extension would use to reach an outside notes service.
        # SecretStr, so its value is redacted everywhere it surfaces; optional with a
        # length floor, so unset is None and a too-short key is rejected server-side
        # (with its raw value kept out of the error).
        sync_token: SecretStr | None = Field(
            default=None,
            min_length=8,
            title="Sync token",
            description="API key for syncing notes to an external service.",
        )

        @field_validator("sync_token")
        @classmethod
        def _well_formed_token(cls, value: SecretStr | None) -> SecretStr | None:
            # A format check whose message names the offending value — the platform
            # must keep that raw value out of the surfaced error for a secret field.
            if value is not None and not value.get_secret_value().startswith("sk-"):
                raise ValueError(f"sync token {value.get_secret_value()!r} must start with 'sk-'")
            return value

    # The one agent this extension runs: it reads a note and writes its summary.
    summarize = Agent(
        description="reads a note and writes a one-line summary",
        prompt="field_notes/summarize.md",
        contract=NoteSummary,
        model="claude",
    )

    @classmethod
    def get_subject_summary(cls, subject: Subject) -> NoteView | None:
        note = Note.get_for_subject(subject)
        if note:
            return NoteView.from_note(note)
        return

    @classmethod
    def list_subjects(cls) -> list[NoteView]:
        notes = Note.list_recent(limit=cls.settings().board_size)
        return [NoteView.from_note(note) for note in notes]

    # The extension's own precondition, reported by `druks doctor` beside the
    # platform's: the summarizer's API key must be set.
    checks = [check_summary_api_key]

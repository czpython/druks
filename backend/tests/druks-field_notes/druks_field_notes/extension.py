import os
from typing import Literal

from druks.agents import Agent
from druks.doctor import CheckResult
from druks.extensions import Extension, ExtensionSettings, Secret
from pydantic import Field, SecretStr, field_validator

from druks_field_notes.contracts import GistOutput

# The env var field_notes would read its summarizer credential from. Unset in a
# bare install, so the check below reports it missing — the "extension owns a check
# for its own API key" case, kept to an env read so the proof package needs no real
# provider.
API_KEY_ENV = "FIELD_NOTES_API_KEY"


def check_summary_api_key() -> CheckResult:
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
    icon = "notebook"
    description = "Turns a jotted observation into a one-line gist with an agent."

    class Settings(ExtensionSettings):
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
        # SecretStr, so its value is redacted everywhere it surfaces; empty means
        # unset, and a malformed key is rejected server-side (with its raw value
        # kept out of the error).
        sync_token: Secret = Field(
            title="Sync token",
            description="API key for syncing notes to an external service.",
            json_schema_extra={
                "section": "Sharing",
                "visible_when": {"visibility": "public"},
            },
        )

        @field_validator("sync_token")
        @classmethod
        def _well_formed_token(cls, value: SecretStr) -> SecretStr:
            # A format check whose message names the offending value — the platform
            # must keep that raw value out of the surfaced error for a secret field.
            if value and not value.get_secret_value().startswith("sk-"):
                raise ValueError(f"sync token {value.get_secret_value()!r} must start with 'sk-'")
            return value

        def clean(self) -> dict[str, str]:
            if self.visibility == "public" and not self.sync_token:
                return {"sync_token": "Required when visibility is public."}
            return {}

    # The one agent this extension runs: it reads a note and writes its gist.
    summarize = Agent(
        description="reads a note and writes its one-line gist",
        prompt="field_notes/summarize.md",
        contract=GistOutput,
        model="claude",
    )

    # The extension's own precondition, reported by `druks doctor` beside the
    # platform's: the summarizer's API key must be set.
    checks = [check_summary_api_key]

import os
from typing import Annotated, Literal

from druks.agents import Agent
from druks.apps import App, AppSettings, Choices, Secret
from druks.doctor import CheckResult
from pydantic import Field, SecretStr, field_validator

from druks_field_notes.contracts import GistOutput

# The summarizer credential. A bare install leaves it unset, so the check reports it.
API_KEY_ENV = "FIELD_NOTES_API_KEY"


def check_summary_api_key() -> CheckResult:
    """Report a missing summarizer credential before the first run fails."""
    if os.environ.get(API_KEY_ENV):
        return CheckResult(name="summary_api_key", ok=True, detail="set")
    return CheckResult(
        name="summary_api_key",
        ok=False,
        detail=f"{API_KEY_ENV} is unset — the summarize agent can't authenticate.",
    )


async def list_notebook_choices() -> list[dict[str, str]]:
    """The notebooks a note can go into. A real app reads them from its service."""
    return [
        {"value": "field", "label": "Field notebook", "group": "Notebooks"},
        {"value": "lab", "label": "Lab notebook", "group": "Notebooks"},
    ]


class FieldNotes(App):
    name = "field_notes"
    icon = "notebook"
    description = "Turns a jotted observation into a one-line gist with an agent."
    navigation = ["notes"]

    class Settings(AppSettings):
        # How many recent notes the board shows.
        board_size: int = Field(
            default=50,
            ge=1,
            le=500,
            title="Board size",
            description="Most-recent notes shown on the field-notes board.",
        )
        # A Literal, so the settings page renders a select.
        visibility: Literal["private", "team", "public"] = Field(
            default="private",
            title="Visibility",
            description="Who the field-notes board is shared with.",
        )
        # Live choices, so the settings page renders a select from the source.
        notebook: Annotated[str, Choices(list_notebook_choices)] = Field(
            default="field",
            title="Notebook",
            description="The notebook that new notes go into.",
        )
        # A multiline secret keeps the newlines of a pasted PEM key.
        sync_signing_key: Secret = Field(
            title="Sync signing key",
            description="PEM key used to sign notes synced to the external service.",
            json_schema_extra={
                "section": "Sharing",
                "visible_when": {"visibility": ["public"]},
                "multiline": True,
            },
        )
        # A secret. Druks redacts its value everywhere, and empty means unset.
        sync_token: Secret = Field(
            title="Sync token",
            description="API key for syncing notes to an external service.",
            json_schema_extra={
                "section": "Sharing",
                "visible_when": {"visibility": ["public"]},
            },
        )

        @field_validator("sync_token")
        @classmethod
        def _well_formed_token(cls, value: SecretStr) -> SecretStr:
            # The message names the raw value. The platform keeps it out of the surfaced error.
            if value and not value.get_secret_value().startswith("sk-"):
                raise ValueError(f"sync token {value.get_secret_value()!r} must start with 'sk-'")
            return value

        def clean(self) -> dict[str, str]:
            if self.visibility == "public" and not self.sync_token:
                return {"sync_token": "Required when visibility is public."}
            return {}

    summarize = Agent(
        description="reads a note and writes its one-line gist",
        prompt="field_notes/summarize.md",
        contract=GistOutput,
    )
    survey = Agent(
        description="reads a cloned repository and writes its one-line gist",
        prompt="field_notes/survey.md",
        contract=GistOutput,
    )

    # `druks doctor` reports this precondition beside the platform checks.
    checks = [check_summary_api_key]

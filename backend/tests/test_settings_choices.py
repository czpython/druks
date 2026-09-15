from typing import Annotated, Literal

import pytest
from druks.apps import App, AppSettings, Choices
from druks.apps.exceptions import SettingsDeclarationError
from druks.apps.loader import register_workflow_package
from druks.apps.settings import validate_field_choice_details
from druks.durable.exceptions import WorkflowError
from druks.user_settings.reads import list_live_choices
from druks.workflows import Workflow
from pydantic import BaseModel, Field


def test_choice_details_preserve_stored_values():
    class Settings(AppSettings):
        policy: Literal["human", "machine"] = Field(
            default="human",
            json_schema_extra={
                "choice_details": {"human": {"label": "Human review", "help": "Wait for approval."}}
            },
        )

    assert validate_field_choice_details(Settings.model_fields["policy"]) == {
        "human": {"label": "Human review", "help": "Wait for approval."}
    }
    assert Settings().policy == "human"


@pytest.mark.parametrize("annotation", [Literal["human", "machine"], str])
def test_a_key_outside_the_declared_choices_fails_at_declaration(annotation):
    with pytest.raises(SettingsDeclarationError, match="not declared choices"):

        class InvalidChoices(App):
            name = "invalid_choices"

            class Settings(AppSettings):
                policy: annotation = Field(
                    default="human",
                    json_schema_extra={
                        "choice_details": {"humna": {"label": "Human", "help": "Wait"}}
                    },
                )


@pytest.mark.parametrize("metadata", [None, {}, lambda schema: schema.update(title="Policy")])
def test_choice_metadata_ignores_absent_values_and_schema_callbacks(metadata):
    class MetadataApp(App):
        name = "choices"

        class Settings(AppSettings):
            policy: Literal["human", "machine"] = Field(default="human", json_schema_extra=metadata)

    assert validate_field_choice_details(MetadataApp.Settings.model_fields["policy"]) == {}


STATUSES = [
    {"value": "todo", "label": "Todo", "group": "To Do"},
    {"value": "done", "label": "Done", "group": "Done"},
]
SOURCE_CALLS = []


async def _statuses():
    SOURCE_CALLS.append(True)
    return STATUSES


async def _no_statuses():
    return []


class _LiveSettings(AppSettings):
    status: Annotated[str, Choices(_statuses)] = "todo"
    resting: Annotated[str, Choices(_statuses)] = ""
    offline: Annotated[str, Choices(_no_statuses)] = ""


async def test_live_choices_call_each_source_once_and_skip_empty_fields():
    SOURCE_CALLS.clear()

    assert await list_live_choices(_LiveSettings) == {
        "status": [{"value": "", "label": ""}, *STATUSES],
        "resting": [{"value": "", "label": ""}, *STATUSES],
    }
    assert len(SOURCE_CALLS) == 1


@pytest.mark.parametrize(
    "annotation", [Annotated[int, Choices(_statuses)], Annotated[str, Choices(_statuses)] | None]
)
def test_choices_on_a_field_that_is_not_a_str_fail_at_declaration(annotation):
    with pytest.raises(SettingsDeclarationError, match="only to a str field"):

        class InvalidLiveChoices(App):
            name = "invalid_live_choices"

            class Settings(AppSettings):
                value: annotation = None


def test_choices_on_workflow_settings_fail_at_declaration():
    register_workflow_package(__name__, "")

    with pytest.raises(WorkflowError, match="only to app settings"):

        class LiveChoicesWorkflow(Workflow):
            class Settings(BaseModel):
                status: Annotated[str, Choices(_statuses)] = "todo"

            async def run(self) -> None: ...

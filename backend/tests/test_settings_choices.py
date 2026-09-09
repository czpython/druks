from typing import Literal

import pytest
from druks.apps import App, AppSettings
from druks.apps.exceptions import SettingsDeclarationError
from druks.apps.settings import validate_field_choice_details
from pydantic import Field


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
    class Choices(App):
        name = "choices"

        class Settings(AppSettings):
            policy: Literal["human", "machine"] = Field(default="human", json_schema_extra=metadata)

    assert validate_field_choice_details(Choices.Settings.model_fields["policy"]) == {}

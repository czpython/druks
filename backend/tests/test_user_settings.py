from typing import Literal

import pytest
from druks.extensions.exceptions import SettingsDeclarationError
from druks.extensions.settings import (
    coerce_setting_value,
    validate_setting_override,
    validate_settings_declaration,
)
from druks.user_settings.models import HarnessSettings, UserSettings
from druks.user_settings.schemas import SettingsFieldResponse
from pydantic import BaseModel, Field, SecretStr, field_validator


@pytest.fixture
def session(druks_db):
    # The per-test connection session (rolled back at teardown); reference rows
    # (the seeded harnesses) come from the session-scoped schema build.
    return druks_db


def test_get_lazy_creates_row_with_default_timezone(session):
    row = UserSettings.get()
    session.commit()
    assert row.timezone == "UTC"


def test_harnesses_seeded_with_shipped_defaults(session):
    # init_db seeds one HarnessSettings row per registered harness.
    claude = HarnessSettings.require("claude")
    assert (claude.model, claude.fast_mode, claude.effort, claude.timeout) == (
        "claude-opus-4-7",
        False,
        "high",
        1800,
    )
    assert HarnessSettings.require("codex").model == "gpt-5.5"
    assert {harness.name for harness in HarnessSettings.all()} == {"claude", "codex"}


def test_harness_update_persists(session):
    HarnessSettings.require("claude").update(model="claude-sonnet-4-6", fast_mode=True)
    session.commit()
    claude = HarnessSettings.require("claude")
    assert claude.model == "claude-sonnet-4-6"
    assert claude.fast_mode is True


class _Declared(BaseModel):
    flag: bool = False
    count: int = Field(default=1, ge=0)
    label: str = ""
    choice: Literal["a", "b", "c"] = "a"
    numeric_choice: Literal[1, 2, 3] = 1
    optional_choice: Literal["x", "y"] | None = None
    # Optional so an unset secret resolves to None — a plain SecretStr with a length
    # floor and an empty default couldn't satisfy its own constraint.
    secret: SecretStr | None = Field(default=None, min_length=8)

    @field_validator("secret")
    @classmethod
    def _reject_forbidden_token(cls, value: SecretStr | None) -> SecretStr | None:
        # A custom validator that embeds the raw value in its message — the redaction
        # must keep this out of the surfaced error for a secret field.
        if value is not None and "forbidden" in value.get_secret_value():
            raise ValueError(f"token {value.get_secret_value()} is not allowed")
        return value


def _resolved() -> dict:
    return _Declared().model_dump()


def _field(name: str, *, value: object) -> dict:
    field = SettingsFieldResponse.from_field(
        name, _Declared.model_fields[name], value=value, overridden=False
    )
    return field.model_dump(by_alias=True)


def test_scalar_fields_project_their_wire_kind():
    assert _field("flag", value=True)["type"] == "bool"
    assert _field("count", value=3)["type"] == "int"
    assert _field("label", value="hi")["type"] == "str"


def test_enum_field_exposes_its_choices():
    projected = _field("choice", value="b")
    assert projected["type"] == "enum"
    assert projected["choices"] == ["a", "b", "c"]
    assert projected["value"] == "b"


def test_secret_field_redacts_value_and_default_and_reports_set():
    unset = _field("secret", value="")
    assert unset["type"] == "secret"
    assert unset["value"] is None
    assert unset["default"] is None
    assert unset["secretSet"] is False

    setted = _field("secret", value="sk-raw")
    assert setted["value"] is None
    assert setted["secretSet"] is True
    assert "sk-raw" not in str(setted)


def test_optional_secret_is_still_treated_as_a_secret():
    # SecretStr inside a union must not slip through as a plaintext string.
    projected = _field("secret", value="sk-raw")
    assert projected["type"] == "secret"
    assert projected["value"] is None
    assert "sk-raw" not in str(projected)


def test_optional_enum_exposes_its_choices():
    # A Literal inside a union is still an enum with its choices surfaced.
    projected = _field("optional_choice", value="y")
    assert projected["type"] == "enum"
    assert projected["choices"] == ["x", "y"]


def test_non_string_enum_choices_are_stringified_for_the_wire():
    projected = _field("numeric_choice", value=2)
    assert projected["type"] == "enum"
    assert projected["choices"] == ["1", "2", "3"]


class _RichlyDeclared(BaseModel):
    split_enum: Literal["a"] | Literal["b"] | None = None
    secret_list: list[SecretStr] = []


def test_union_of_separate_literals_collects_every_member():
    field = _RichlyDeclared.model_fields["split_enum"]
    projected = SettingsFieldResponse.from_field(
        "split_enum", field, value="b", overridden=False
    ).model_dump(by_alias=True)
    assert projected["type"] == "enum"
    assert projected["choices"] == ["a", "b"]


def test_secret_inside_a_container_is_redacted():
    # A list of secrets is a legitimate declaration — classify it secret and redact it,
    # not project the raw values.
    field = _RichlyDeclared.model_fields["secret_list"]
    projected = SettingsFieldResponse.from_field(
        "secret_list", field, value=["sk-one", "sk-two"], overridden=False
    ).model_dump(by_alias=True)
    assert projected["type"] == "secret"
    assert projected["value"] is None
    assert projected["default"] is None
    assert "sk-one" not in str(projected)


def test_nested_model_settings_field_is_rejected_at_declaration():
    class _Inner(BaseModel):
        token: SecretStr | None = None

    class _NestedSettings(BaseModel):
        inner: _Inner = _Inner()

    with pytest.raises(SettingsDeclarationError, match="inner"):
        validate_settings_declaration(_NestedSettings)


def test_coerce_maps_a_submitted_string_back_to_the_literal_member_type():
    # A select submits every choice as a string; a numeric Literal needs the int back.
    assert coerce_setting_value(_Declared, "numeric_choice", "2") == 2
    assert isinstance(coerce_setting_value(_Declared, "numeric_choice", "2"), int)
    # A string Literal and non-enum fields pass through untouched.
    assert coerce_setting_value(_Declared, "choice", "b") == "b"
    assert coerce_setting_value(_Declared, "count", 3) == 3
    # A value naming no member is left as-is for validation to reject.
    assert coerce_setting_value(_Declared, "numeric_choice", "9") == "9"


def test_validate_setting_override_accepts_a_coerced_numeric_enum():
    value = coerce_setting_value(_Declared, "numeric_choice", "2")
    validate_setting_override(_Declared, _resolved(), "numeric_choice", value)


def test_validate_setting_override_runs_against_resolved_state_not_a_blank_shell():
    # An unrelated change validates against the currently-resolved settings; a sibling
    # (the optional secret, unset) doesn't spuriously fail the whole model.
    validate_setting_override(_Declared, _resolved(), "count", 3)


def test_validate_setting_override_never_echoes_the_submitted_value():
    # A rejected value — especially a secret — must not appear in the error message.
    with pytest.raises(ValueError) as enum_error:
        validate_setting_override(_Declared, _resolved(), "choice", "GALAXY-SECRET")
    assert "choice" in str(enum_error.value)
    assert "GALAXY-SECRET" not in str(enum_error.value)

    with pytest.raises(ValueError) as secret_error:
        validate_setting_override(_Declared, _resolved(), "secret", "skRAW")
    assert "secret" in str(secret_error.value)
    assert "skRAW" not in str(secret_error.value)


def test_secret_field_custom_validator_message_is_not_echoed():
    # A custom validator can embed the raw value in its ValueError message; for a
    # secret field that message must be replaced, never surfaced.
    with pytest.raises(ValueError) as error:
        validate_setting_override(_Declared, _resolved(), "secret", "forbidden-key-1")
    assert "secret" in str(error.value)
    assert "forbidden-key-1" not in str(error.value)

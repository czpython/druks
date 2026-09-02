from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, PositiveInt
from pydantic.fields import FieldInfo

from druks.apps.settings import (
    field_choices,
    field_kind,
    field_multiline,
    field_section,
    field_visibility,
)
from druks.harnesses.schemas import SortedNames
from druks.schemas import Schema

from .datastructures import Effort

if TYPE_CHECKING:
    pass


class HarnessResponse(Schema):
    model_config = ConfigDict(from_attributes=True)

    name: str
    # The provider a subscription CLI is bound to; None for a key CLI.
    provider: str | None
    login_kinds: SortedNames
    effort: str
    timeout: int
    fast_mode: bool


class HarnessUpdate(BaseModel):
    fast_mode: bool | None = Field(default=None, validation_alias="fastMode")
    effort: Effort | None = None
    timeout: PositiveInt | None = None


class UserSettingsResponse(Schema):
    model_config = ConfigDict(from_attributes=True)

    timezone: str
    default_model: str
    gate_park_destination_id: str | None
    updated_at: datetime


class UpdateUserSettingsRequest(BaseModel):
    timezone: str | None = None
    default_model: str | None = Field(default=None, validation_alias="defaultModel")
    # Tri-state: absent = unchanged, null = clear (off), value = designate.
    gate_park_destination_id: str | None = Field(
        default=None, validation_alias="gateParkDestinationId"
    )


class AgentSettingResponse(Schema):
    name: str
    description: str
    model: str
    source: Literal["agent", "default"]
    effort: str
    effort_source: Literal["agent", "harness"]
    timeout: int
    timeout_source: Literal["agent", "declared", "harness"]


class SettingsFieldResponse(Schema):
    name: str
    # Human label + one-line help, from the field's ``Field(title=, description=)``.
    label: str
    help: str
    type: str
    # A secret field carries neither its stored value nor its default here — only
    # whether one is set — so a raw secret can't ride out in any response.
    value: Any
    default: Any
    # An enum field's allowed values; None for every other kind.
    choices: list[str] | None
    # The heading this field groups under; empty for an ungrouped one.
    section: str
    # The sibling field this one is shown for, and the value that field must hold. The
    # name is empty when the field is always shown.
    visible_when_field: str
    visible_when_value: Any
    # For a secret field, whether a non-empty value is currently stored (override or
    # default). None for every other kind — the UI shows a "set / not set" hint only
    # for secrets.
    secret_set: bool | None
    # The value carries meaningful newlines (a pasted PEM) — the UI renders a
    # textarea. Presentation only; declared via json_schema_extra.
    multiline: bool = False
    overridden: bool

    @classmethod
    def from_field(
        cls, name: str, field: FieldInfo, *, value: Any, overridden: bool
    ) -> "SettingsFieldResponse":
        kind = field_kind(field)
        secret = kind == "secret"
        controller, target = field_visibility(field)
        return cls(
            name=name,
            label=field.title or name,
            help=field.description or "",
            type=kind,
            value=None if secret else value,
            default=None if secret else field.default,
            choices=field_choices(field),
            section=field_section(field),
            visible_when_field=controller,
            visible_when_value=target,
            secret_set=bool(value) if secret else None,
            multiline=field_multiline(field),
            overridden=overridden,
        )


class WorkflowSettingsResponse(Schema):
    kind: str
    fields: list[SettingsFieldResponse]


class AppSettingsResponse(Schema):
    name: str
    description: str
    # A Lucide icon name the frontend renders (falls back to a default if unknown).
    icon: str
    # Built-in (platform-core) apps' agents are shown under the Druks tab, not
    # a tab of their own.
    builtin: bool
    agents: list[AgentSettingResponse]
    workflows: list[WorkflowSettingsResponse]
    # The app's own declared settings (not tied to a workflow). Rendered
    # in the same options section as workflow ones.
    settings: list[SettingsFieldResponse]


class AppsSettingsResponse(Schema):
    allowed_efforts: list[str]
    apps: list[AppSettingsResponse]


class AppsSettingsUpdate(BaseModel):
    # agent name -> model (null clears, i.e. inherit the operator default).
    agent_models: dict[str, str | None] = Field(
        default_factory=dict,
        validation_alias="agentModels",
    )
    # agent name -> effort (null clears, i.e. inherit the harness default).
    agent_efforts: dict[str, Effort | None] = Field(
        default_factory=dict,
        validation_alias="agentEfforts",
    )
    # agent name -> timeout seconds (null clears, i.e. inherit the harness default).
    agent_timeouts: dict[str, PositiveInt | None] = Field(
        default_factory=dict,
        validation_alias="agentTimeouts",
    )
    # workflow kind -> {field -> value} (null clears).
    workflow_settings: dict[str, dict[str, Any]] = Field(
        default_factory=dict,
        validation_alias="workflowSettings",
    )
    # app name -> {field -> value} (null clears).
    app_settings: dict[str, dict[str, Any]] = Field(
        default_factory=dict,
        validation_alias="appSettings",
    )

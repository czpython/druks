from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, PositiveInt
from pydantic.fields import FieldInfo

from druks.apps.settings import (
    field_choices,
    field_kind,
    field_multiline,
    field_section,
    field_visibility,
    validate_field_choice_details,
)
from druks.core.utils.time import validate_timezone
from druks.harnesses.datastructures import Billing
from druks.harnesses.schemas import SortedNames
from druks.schemas import Schema

from .datastructures import Effort


class HarnessResponse(Schema):
    model_config = ConfigDict(from_attributes=True)

    name: str
    provider: str | None
    billing_options: SortedNames


class PersonalSettingsResponse(Schema):
    model_config = ConfigDict(from_attributes=True)

    timezone: str
    gate_park_destination_id: str | None


class SettingsResponse(Schema):
    model_config = ConfigDict(from_attributes=True)

    gate_park_destination_id: str | None
    updated_at: datetime
    default_harness: str
    default_model: str
    default_billing: str
    default_effort: str
    fast_mode: bool
    default_timeout: int


class UpdatePersonalSettingsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timezone: Annotated[str, AfterValidator(validate_timezone)] | None = None
    # Absent leaves the destination unchanged. Null turns notifications off.
    gate_park_destination_id: str | None = Field(
        default=None, validation_alias="gateParkDestinationId"
    )


class UpdateSettingsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    gate_park_destination_id: str | None = Field(
        default=None, validation_alias="gateParkDestinationId"
    )
    default_harness: str | None = Field(default=None, validation_alias="defaultHarness")
    default_model: str | None = Field(default=None, validation_alias="defaultModel")
    default_billing: Billing | None = Field(default=None, validation_alias="defaultBilling")
    default_effort: Effort | None = Field(default=None, validation_alias="defaultEffort")
    fast_mode: bool | None = Field(default=None, validation_alias="fastMode")
    default_timeout: PositiveInt | None = Field(default=None, validation_alias="defaultTimeout")


Source = Literal["agent", "default"]


class AgentSettingResponse(Schema):
    name: str
    label: str
    description: str
    harness: str
    harness_source: Source
    model: str
    source: Source
    billing: str
    billing_source: Source
    effort: str
    effort_source: Source
    timeout: int
    timeout_source: Literal["agent", "declared", "default"]


class AgentsAppResponse(Schema):
    name: str
    agents: list[AgentSettingResponse]


class AgentsResponse(Schema):
    apps: list[AgentsAppResponse]


class SettingsFieldResponse(Schema):
    name: str
    label: str
    help: str
    type: str
    # A secret field sends neither its value nor its default. secret_set says whether it is set.
    value: Any
    default: Any
    # The allowed values of an enum field.
    choices: list[str] | None
    choice_details: dict[str, dict[str, str]] = {}
    section: str
    visible_when_field: str
    visible_when_values: list[Any]
    secret_set: bool | None
    multiline: bool = False
    overridden: bool

    @classmethod
    def from_field(
        cls, name: str, field: FieldInfo, *, value: Any, overridden: bool
    ) -> "SettingsFieldResponse":
        kind = field_kind(field)
        secret = kind == "secret"
        controller, targets = field_visibility(field)
        return cls(
            name=name,
            label=field.title or name,
            help=field.description or "",
            type=kind,
            value=None if secret else value,
            default=None if secret else field.default,
            choices=field_choices(field),
            choice_details=validate_field_choice_details(field),
            section=field_section(field),
            visible_when_field=controller,
            visible_when_values=targets,
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
    # A Lucide icon name.
    icon: str
    # The agents of a built-in app show under the Druks tab.
    builtin: bool
    # The id of the app's Bot, which answers people on its channels.
    bot: str | None
    agents: list[AgentSettingResponse]
    workflows: list[WorkflowSettingsResponse]
    settings: list[SettingsFieldResponse]


class AppsSettingsResponse(Schema):
    allowed_efforts: list[str]
    apps: list[AppSettingsResponse]


class AppsSettingsUpdate(BaseModel):
    # Each map is agent name to value. Null inherits the operator default.
    agent_harnesses: dict[str, str | None] = Field(
        default_factory=dict,
        validation_alias="agentHarnesses",
    )
    agent_models: dict[str, str | None] = Field(
        default_factory=dict,
        validation_alias="agentModels",
    )
    agent_billings: dict[str, Billing | None] = Field(
        default_factory=dict,
        validation_alias="agentBillings",
    )
    agent_efforts: dict[str, Effort | None] = Field(
        default_factory=dict,
        validation_alias="agentEfforts",
    )
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

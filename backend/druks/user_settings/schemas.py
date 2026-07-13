from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic.fields import FieldInfo

from druks.extensions.settings import field_choices, field_kind
from druks.schemas import BaseResponse

if TYPE_CHECKING:
    from druks.harnesses.models import HarnessLogin
    from druks.user_settings.models import HarnessSettings


class HarnessResponse(BaseResponse):
    name: str
    provider: str
    model: str
    effort: str
    timeout: int
    fast_mode: bool
    allowed_models: list[str]
    # Connection state, joined in from the HarnessLogin row — connected=False
    # until the operator connects the harness from the dashboard.
    connected: bool
    kind: str | None
    account: str | None
    expires_at: datetime | None

    @classmethod
    def from_row(
        cls, settings: "HarnessSettings", login: "HarnessLogin | None"
    ) -> "HarnessResponse":
        return cls(
            name=settings.name,
            provider=settings.provider,
            model=settings.model,
            effort=settings.effort,
            timeout=settings.timeout,
            fast_mode=settings.fast_mode,
            allowed_models=settings.allowed_models,
            connected=bool(login),
            kind=login.kind if login else None,
            account=login.account if login else None,
            expires_at=login.expires_at if login else None,
        )


class HarnessUpdate(BaseModel):
    model: str | None = None
    fast_mode: bool | None = Field(default=None, validation_alias="fastMode")
    effort: str | None = None
    timeout: int | None = None


class UserSettingsResponse(BaseResponse):
    model_config = ConfigDict(from_attributes=True)

    timezone: str
    gate_park_destination_id: str | None
    updated_at: datetime


class UpdateUserSettingsRequest(BaseModel):
    timezone: str | None = None
    # Tri-state: absent = unchanged, null = clear (off), value = designate.
    gate_park_destination_id: str | None = Field(
        default=None, validation_alias="gateParkDestinationId"
    )


class AgentSettingResponse(BaseResponse):
    name: str
    description: str
    model: str
    source: Literal["agent", "default"]
    # The declared default — a family token (codex/claude) the model resolves to.
    default: str
    effort: str
    effort_source: Literal["agent", "declared", "harness"]
    timeout: int
    timeout_source: Literal["agent", "declared", "harness"]


class SettingsFieldResponse(BaseResponse):
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
    # For a secret field, whether a non-empty value is currently stored (override or
    # default). None for every other kind — the UI shows a "set / not set" hint only
    # for secrets.
    secret_set: bool | None
    overridden: bool

    @classmethod
    def from_field(
        cls, name: str, field: FieldInfo, *, value: Any, overridden: bool
    ) -> "SettingsFieldResponse":
        kind = field_kind(field)
        secret = kind == "secret"
        return cls(
            name=name,
            label=field.title or name,
            help=field.description or "",
            type=kind,
            value=None if secret else value,
            default=None if secret else field.default,
            choices=field_choices(field),
            secret_set=bool(value) if secret else None,
            overridden=overridden,
        )


class WorkflowSettingsResponse(BaseResponse):
    kind: str
    fields: list[SettingsFieldResponse]


class ExtensionSettingsResponse(BaseResponse):
    name: str
    description: str
    # A Lucide icon name the frontend renders (falls back to a default if unknown).
    icon: str
    # Built-in (platform-core) extensions' agents are shown under the Druks tab, not
    # a tab of their own.
    builtin: bool
    agents: list[AgentSettingResponse]
    workflows: list[WorkflowSettingsResponse]
    # The extension's own declared settings (not tied to a workflow). Rendered
    # in the same options section as workflow ones.
    settings: list[SettingsFieldResponse]


class ExtensionsSettingsResponse(BaseResponse):
    allowed_efforts: list[str]
    extensions: list[ExtensionSettingsResponse]


class ExtensionsSettingsUpdate(BaseModel):
    # agent name -> model (null clears, i.e. inherit the family default).
    agent_models: dict[str, str | None] = Field(
        default_factory=dict,
        validation_alias="agentModels",
    )
    # agent name -> effort (null clears, i.e. inherit the harness default).
    agent_efforts: dict[str, str | None] = Field(
        default_factory=dict,
        validation_alias="agentEfforts",
    )
    # agent name -> timeout seconds (null clears, i.e. inherit the harness default).
    agent_timeouts: dict[str, int | None] = Field(
        default_factory=dict,
        validation_alias="agentTimeouts",
    )
    # workflow kind -> {field -> value} (null clears).
    workflow_settings: dict[str, dict[str, Any]] = Field(
        default_factory=dict,
        validation_alias="workflowSettings",
    )
    # extension name -> {field -> value} (null clears).
    extension_settings: dict[str, dict[str, Any]] = Field(
        default_factory=dict,
        validation_alias="extensionSettings",
    )

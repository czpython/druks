# The settings read side: resolve declared defaults through the override store
# into the wire shapes. Schemas stay pure projections.
from typing import TYPE_CHECKING, Any

from pydantic.fields import FieldInfo

from druks.apps.settings import field_kind
from druks.database import db_session

from .models import SettingsOverride
from .schemas import (
    AgentSettingResponse,
    AppSettingsResponse,
    SettingsFieldResponse,
    WorkflowSettingsResponse,
)

if TYPE_CHECKING:
    from druks.agents import Agent
    from druks.apps import App
    from druks.workflows import Workflow


async def get_agent_setting(agent: "Agent") -> AgentSettingResponse:
    harness = await SettingsOverride.agent_harness(agent.id)
    model = await SettingsOverride.agent_model(agent.id)
    billing = await SettingsOverride.agent_billing(agent.id)
    effort = await SettingsOverride.agent_effort(agent.id)
    timeout = await SettingsOverride.agent_timeout(agent.id, agent.timeout)
    return AgentSettingResponse(
        name=agent.name or agent.id,
        description=agent.description,
        harness=harness.value,
        harness_source=harness.source,
        model=model.value,
        source=model.source,
        billing=billing.value,
        billing_source=billing.source,
        effort=effort.value,
        effort_source=effort.source,
        timeout=timeout.value,
        timeout_source=timeout.source,
    )


async def get_settings_field(
    name: str, field: FieldInfo, *, value: Any, override_key: str
) -> SettingsFieldResponse:
    overridden = bool(await db_session().get(SettingsOverride, override_key))
    return SettingsFieldResponse.from_field(name, field, value=value, overridden=overridden)


async def get_workflow_settings(workflow: "type[Workflow]") -> WorkflowSettingsResponse:
    kind = workflow.kind
    fields = [
        await get_settings_field(
            name,
            field,
            value=await SettingsOverride.workflow_setting(kind, name, field.default),
            override_key=f"workflow:{kind}:{name}",
        )
        for name, field in workflow.Settings.model_fields.items()
    ]
    if workflow.every:
        # The schedule pair renders like any declared field. The label carries
        # the workflow's name since an app's fields show as one flat list.
        label = kind.rsplit(".", 1)[-1].replace("_", " ")
        fields += [
            SettingsFieldResponse(
                name="schedule",
                label=f"{label} schedule",
                help="How often the scheduled run fires, in your configured timezone.",
                # "cron" is a UI kind like enum/secret: the frontend renders
                # cadence presets with a raw-cron escape hatch.
                type="cron",
                value=await workflow.get_schedule(),
                default=workflow.every,
                choices=None,
                section="",
                visible_when_field="",
                visible_when_value=None,
                secret_set=None,
                overridden=await SettingsOverride.read(f"workflow:{kind}:schedule") is not None,
            ),
            SettingsFieldResponse(
                name="schedule_enabled",
                label=f"{label} enabled",
                help="Pause the scheduled run without losing its cadence.",
                type="bool",
                value=await workflow.has_enabled_schedule(),
                default=True,
                choices=None,
                section="",
                visible_when_field="",
                visible_when_value=None,
                secret_set=None,
                overridden=await SettingsOverride.read(f"workflow:{kind}:schedule_enabled")
                is not None,
            ),
        ]
    return WorkflowSettingsResponse(kind=kind, fields=fields)


async def get_app_settings(app: "type[App]") -> AppSettingsResponse:
    model = app.settings_model
    return AppSettingsResponse(
        name=app.name,
        description=app.description,
        icon=app.icon,
        builtin=app.builtin,
        agents=[await get_agent_setting(agent) for agent in app.agents()],
        # Surface only the workflows with operator knobs: tunable settings or a
        # schedule to retune.
        workflows=[
            await get_workflow_settings(workflow)
            for workflow in app.workflows()
            if workflow.Settings.model_fields or workflow.every
        ],
        settings=[
            await get_settings_field(
                name,
                field,
                value=await SettingsOverride.app_setting(
                    app.name,
                    name,
                    field.default,
                    is_secret=field_kind(field) == "secret",
                ),
                override_key=f"app:{app.name}:{name}",
            )
            for name, field in (model.model_fields if model else {}).items()
        ],
    )

# The settings read side: resolve declared defaults through the override store
# into the wire shapes. Schemas stay pure projections.
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel
from pydantic.fields import FieldInfo
from sqlalchemy.ext.asyncio import AsyncSession

from druks.apps.settings import field_kind, field_live_choices

from .models import InstallationSettings, SettingsOverride
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


async def get_agent_setting(
    session: AsyncSession, agent: "Agent", *, settings: InstallationSettings
) -> AgentSettingResponse:
    harness = await SettingsOverride.agent_harness(session, agent.id, settings=settings)
    model = await SettingsOverride.agent_model(session, agent.id, settings=settings)
    billing = await SettingsOverride.agent_billing(session, agent.id, settings=settings)
    effort = await SettingsOverride.agent_effort(session, agent.id, settings=settings)
    timeout = await SettingsOverride.agent_timeout(
        session, agent.id, agent.timeout, settings=settings
    )
    return AgentSettingResponse(
        name=agent.id,
        label=agent.name or agent.id.rsplit(".", 1)[-1],
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


async def list_live_choices(model: type[BaseModel]) -> dict[str, list[dict[str, str]]]:
    """The live choices of each field that has any, keyed by field name, after an empty
    choice. Fields that share a source share one call."""
    sources = {
        name: live.source
        for name, field in model.model_fields.items()
        if (live := field_live_choices(field))
    }
    results = {source: await source() for source in set(sources.values())}
    return {
        name: [{"value": "", "label": ""}, *results[source]]
        for name, source in sources.items()
        if results[source]
    }


async def get_settings_field(
    session: AsyncSession, name: str, field: FieldInfo, *, value: Any, override_key: str
) -> SettingsFieldResponse:
    overridden = bool(await session.get(SettingsOverride, override_key))
    return SettingsFieldResponse.from_field(name, field, value=value, overridden=overridden)


async def get_workflow_settings(
    session: AsyncSession, workflow: "type[Workflow]"
) -> WorkflowSettingsResponse:
    kind = workflow.kind
    fields = [
        await get_settings_field(
            session,
            name,
            field,
            value=await SettingsOverride.workflow_setting(session, kind, name, field.default),
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
                help="How often the scheduled run fires, in the installation timezone.",
                # "cron" is a UI kind like enum/secret: the frontend renders
                # cadence presets with a raw-cron escape hatch.
                type="cron",
                value=await workflow.get_schedule(session),
                default=workflow.every,
                choices=None,
                section="",
                visible_when_field="",
                visible_when_values=[],
                secret_set=None,
                overridden=await SettingsOverride.read(session, f"workflow:{kind}:schedule")
                is not None,
            ),
            SettingsFieldResponse(
                name="schedule_enabled",
                label=f"{label} enabled",
                help="Pause the scheduled run without losing its cadence.",
                type="bool",
                value=await workflow.has_enabled_schedule(session),
                default=True,
                choices=None,
                section="",
                visible_when_field="",
                visible_when_values=[],
                secret_set=None,
                overridden=await SettingsOverride.read(session, f"workflow:{kind}:schedule_enabled")
                is not None,
            ),
        ]
    return WorkflowSettingsResponse(kind=kind, fields=fields)


async def get_app_settings(
    session: AsyncSession, app: "type[App]", *, settings: InstallationSettings
) -> AppSettingsResponse:
    model = app.settings_model
    return AppSettingsResponse(
        name=app.name,
        description=app.description,
        icon=app.icon,
        builtin=app.builtin,
        agents=[
            await get_agent_setting(session, agent, settings=settings) for agent in app.agents()
        ],
        # Surface only the workflows with operator knobs: tunable settings or a
        # schedule to retune.
        workflows=[
            await get_workflow_settings(session, workflow)
            for workflow in app.workflows()
            if workflow.Settings.model_fields or workflow.every
        ],
        settings=[
            await get_settings_field(
                session,
                name,
                field,
                value=await SettingsOverride.app_setting(
                    session,
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

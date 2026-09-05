from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException

from druks.accounts.dependencies import current_session_account
from druks.accounts.models import Account
from druks.apps.loader import get_app, iter_apps
from druks.apps.registry import agents, workflows
from druks.durable.engine import apply_schedules
from druks.harnesses.base import Harness
from druks.harnesses.execution import check_execution
from druks.harnesses.registry import get_harnesses
from druks.notifications.models import Destination

from . import reads
from .datastructures import ALLOWED_EFFORTS
from .models import SettingsOverride, UserSettings
from .schemas import (
    AgentsAppResponse,
    AgentsResponse,
    AppsSettingsResponse,
    AppsSettingsUpdate,
    HarnessResponse,
    UpdateUserSettingsRequest,
    UserSettingsResponse,
)

router = APIRouter(prefix="/api/settings", tags=["settings"])
agents_router = APIRouter(prefix="/api/agents", tags=["settings"])

_EXECUTION_DEFAULTS = (
    "default_harness",
    "default_model",
    "default_billing",
    "default_effort",
    "fast_mode",
    "default_timeout",
)


def _validate_timezone(value: str) -> str:
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown IANA timezone: {value!r}",
        ) from exc
    return value


@router.get("/harnesses", response_model=list[HarnessResponse], response_model_by_alias=True)
async def list_harnesses() -> tuple[type[Harness], ...]:
    return get_harnesses()


@agents_router.get("", response_model=AgentsResponse, response_model_by_alias=True)
async def list_agents() -> AgentsResponse:
    projected = [
        AgentsAppResponse(
            name=app.name,
            agents=[await reads.get_agent_setting(agent) for agent in app.agents()],
        )
        for app in iter_apps()
    ]
    return AgentsResponse(apps=[app for app in projected if app.agents])


@router.get("", response_model=UserSettingsResponse, response_model_by_alias=True)
async def get_user_settings() -> UserSettings:
    return await UserSettings.get()


@router.patch("", response_model=UserSettingsResponse, response_model_by_alias=True)
async def update_user_settings(
    body: UpdateUserSettingsRequest,
) -> UserSettings:
    row = await UserSettings.get()
    if body.timezone:
        tz = _validate_timezone(body.timezone)
        if tz != row.timezone:
            await row.update_profile(timezone=tz)
            # Crons are evaluated in this timezone — repoint them now, not at
            # the next launch.
            await apply_schedules()
    defaults = {
        field: value
        for field, value in body.model_dump(exclude_unset=True, exclude_none=True).items()
        if field in _EXECUTION_DEFAULTS
    }
    if defaults:
        await check_execution(
            defaults.get("default_harness", row.default_harness),
            defaults.get("default_model", row.default_model),
            defaults.get("default_billing", row.default_billing),
        )
        await row.update_profile(**defaults)
        for agent in agents.all():
            name = agent.id
            await check_execution(
                (await SettingsOverride.agent_harness(name)).value,
                (await SettingsOverride.agent_model(name)).value,
                (await SettingsOverride.agent_billing(name)).value,
            )
    if body.fallback_account_id:
        if not await Account.get(body.fallback_account_id, exclude_system=True):
            raise HTTPException(
                status_code=422, detail=f"Unknown account {body.fallback_account_id!r}"
            )
        await row.set_fallback_account(body.fallback_account_id)
    if "gate_park_destination_id" in body.model_fields_set:
        destination_id = body.gate_park_destination_id
        if destination_id and not await Destination.get(destination_id):
            raise HTTPException(status_code=422, detail=f"Unknown destination {destination_id!r}")
        await row.set_gate_park_destination(destination_id)
    return row


@router.get("/apps", response_model=AppsSettingsResponse, response_model_by_alias=True)
async def get_app_settings() -> AppsSettingsResponse:
    projected = [await reads.get_app_settings(m) for m in iter_apps()]
    return AppsSettingsResponse(
        allowed_efforts=list(ALLOWED_EFFORTS),
        apps=[out for out in projected if out.agents or out.workflows or out.settings],
    )


@router.patch(
    "/apps",
    response_model=AppsSettingsResponse,
    response_model_by_alias=True,
    dependencies=[Depends(current_session_account)],
)
async def update_app_settings(body: AppsSettingsUpdate) -> AppsSettingsResponse:
    for name, harness in body.agent_harnesses.items():
        await SettingsOverride.set_agent_harness(name, harness)
    for name, model in body.agent_models.items():
        await SettingsOverride.set_agent_model(name, model)
    for name, billing in body.agent_billings.items():
        await SettingsOverride.set_agent_billing(name, billing)
    # A cell set alone must still fit the two it inherits, so the check reads
    # the stored triple; a rejection rolls the writes back.
    for name in {*body.agent_harnesses, *body.agent_models, *body.agent_billings}:
        if name not in agents:
            raise HTTPException(status_code=422, detail=f"Unknown agent {name!r}")
        await check_execution(
            (await SettingsOverride.agent_harness(name)).value,
            (await SettingsOverride.agent_model(name)).value,
            (await SettingsOverride.agent_billing(name)).value,
        )

    for name, effort in body.agent_efforts.items():
        await SettingsOverride.set_agent_effort(name, effort)

    for name, timeout in body.agent_timeouts.items():
        await SettingsOverride.set_agent_timeout(name, timeout)

    changed_apps = []
    try:
        for kind, changes in body.workflow_settings.items():
            workflow = workflows.get(kind)
            if not workflow:
                raise HTTPException(status_code=422, detail=f"Unknown workflow {kind!r}")
            for field, value in changes.items():
                await workflow.override_setting(field, value)
        for app_name, changes in body.app_settings.items():
            try:
                app = get_app(app_name)
            except KeyError as exc:
                raise HTTPException(status_code=422, detail=f"Unknown app {app_name!r}") from exc
            for field, value in changes.items():
                await app.override_setting(field, value)
            changed_apps.append(app)
    except ValueError as exc:
        # Domain rejections (unknown field, bad cron, failed constraint) → 422.
        # override_setting has already redacted any submitted value out of the
        # message, so this is safe to surface even for a rejected secret.
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    settings_problems = {}
    for app in changed_apps:
        if problems := (await app.settings()).clean():
            settings_problems[app.name] = problems
    if settings_problems:
        raise HTTPException(status_code=422, detail=settings_problems)

    if any(
        field in ("schedule", "schedule_enabled")
        for changes in body.workflow_settings.values()
        for field in changes
    ):
        # Repoint the DBOS crons now, not at the next launch; the reconcile reads
        # the just-written overrides off this request's session.
        await apply_schedules()

    return await get_app_settings()

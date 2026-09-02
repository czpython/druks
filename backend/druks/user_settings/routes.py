from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException

from druks.accounts.dependencies import current_session_account
from druks.apps.loader import get_app, iter_apps
from druks.apps.registry import workflows
from druks.durable.engine import apply_schedules
from druks.harnesses.registry import get_harness_for_model, get_harnesses
from druks.notifications.models import Destination

from . import reads
from .datastructures import ALLOWED_EFFORTS
from .models import HarnessSettings, SettingsOverride, UserSettings
from .schemas import (
    AppsSettingsResponse,
    AppsSettingsUpdate,
    HarnessResponse,
    HarnessUpdate,
    UpdateUserSettingsRequest,
    UserSettingsResponse,
)

router = APIRouter(prefix="/api/settings", tags=["settings"])


def _validate_timezone(value: str) -> str:
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown IANA timezone: {value!r}",
        ) from exc
    return value


async def _resolve_harness(name: str) -> tuple[type, HarnessSettings]:
    row = await HarnessSettings.get(name)
    if row:
        return row.harness, row
    raise HTTPException(status_code=404, detail=f"Unknown harness: {name!r}")


@router.get("/harnesses", response_model=list[HarnessResponse], response_model_by_alias=True)
async def list_harness_settings() -> list[HarnessResponse]:
    registered = {harness.name for harness in get_harnesses()}
    rows = await HarnessSettings.all()
    return [HarnessResponse.model_validate(row) for row in rows if row.name in registered]


@router.patch("/harnesses/{name}", response_model=HarnessResponse, response_model_by_alias=True)
async def update_harness_settings(name: str, body: HarnessUpdate) -> HarnessResponse:
    harness, row = await _resolve_harness(name)
    updates = body.model_dump(exclude_unset=True, by_alias=False)
    if (model := updates.get("model")) and get_harness_for_model(model) is not harness:
        raise HTTPException(status_code=422, detail=f"{model!r} is not a {harness.name} model.")
    if updates:
        await row.update(**updates)
    return HarnessResponse.model_validate(row)


@router.get("", response_model=UserSettingsResponse, response_model_by_alias=True)
async def get_user_settings() -> UserSettings:
    return await UserSettings.get()


@router.patch("", response_model=UserSettingsResponse, response_model_by_alias=True)
async def update_user_settings(
    body: UpdateUserSettingsRequest,
) -> UserSettings:
    row = await UserSettings.get()
    if body.timezone is not None:
        tz = _validate_timezone(body.timezone)
        if tz != row.timezone:
            await row.update_profile(timezone=tz)
            # Crons are evaluated in this timezone — repoint them now, not at
            # the next launch.
            await apply_schedules()
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
    for name, model in body.agent_models.items():
        if model:
            get_harness_for_model(model)
        await SettingsOverride.set_agent_model(name, model)

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

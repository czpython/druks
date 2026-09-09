from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select

from druks.accounts.dependencies import current_account, current_session_account
from druks.accounts.models import Account
from druks.apps.loader import get_app, iter_apps
from druks.apps.registry import agents, workflows
from druks.database import db_session
from druks.durable.engine import apply_schedules
from druks.harnesses.base import Harness
from druks.harnesses.exceptions import ProfileSettingsError
from druks.harnesses.profiles import check_profile
from druks.harnesses.registry import get_harnesses
from druks.notifications.models import Destination

from . import reads
from .datastructures import ALLOWED_EFFORTS
from .models import SettingsOverride, SettingsProfile
from .schemas import (
    AgentsAppResponse,
    AgentsResponse,
    AppsSettingsResponse,
    AppsSettingsUpdate,
    HarnessResponse,
    SettingsResponse,
    UpdateSettingsRequest,
)

router = APIRouter(prefix="/api/settings", tags=["settings"])
agents_router = APIRouter(prefix="/api/agents", tags=["settings"])

_PROFILE_DEFAULTS = (
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
async def list_agents(account: Account = Depends(current_account)) -> AgentsResponse:
    settings = await SettingsProfile.get(account.id)
    projected = [
        AgentsAppResponse(
            name=app.name,
            agents=[
                await reads.get_agent_setting(agent, settings=settings) for agent in app.agents()
            ],
        )
        for app in iter_apps()
    ]
    return AgentsResponse(apps=[app for app in projected if app.agents])


@router.get("", response_model=SettingsResponse, response_model_by_alias=True)
async def get_settings() -> SettingsProfile:
    return await SettingsProfile.get()


@router.get("/personal", response_model=SettingsResponse, response_model_by_alias=True)
async def get_personal_settings(account: Account = Depends(current_account)) -> SettingsProfile:
    return await SettingsProfile.get(account.id)


async def check_agent_profiles(settings: SettingsProfile) -> None:
    await check_profile(settings.default_harness, settings.default_model, settings.default_billing)
    for agent in agents.all():
        await check_profile(
            (await SettingsOverride.agent_harness(agent.id, settings=settings)).value,
            (await SettingsOverride.agent_model(agent.id, settings=settings)).value,
            (await SettingsOverride.agent_billing(agent.id, settings=settings)).value,
        )


async def save_settings(
    body: UpdateSettingsRequest, account_id: str | None = None
) -> SettingsProfile:
    fields = body.model_dump(exclude_unset=True, exclude_none=True)
    if "timezone" in fields:
        fields["timezone"] = _validate_timezone(fields["timezone"])
    if "gate_park_destination_id" in body.model_fields_set:
        destination_id = body.gate_park_destination_id
        if destination_id and not await Destination.get(destination_id):
            raise HTTPException(status_code=422, detail=f"Unknown destination {destination_id!r}")
        fields["gate_park_destination_id"] = destination_id
    row = await SettingsProfile.get(account_id)
    if fields:
        if account_id and not row.account_id:
            row = await row.copy_for_account(account_id)
        timezone_changed = "timezone" in fields and fields["timezone"] != row.timezone
        await row.update_profile(**fields)
        if any(field in fields for field in _PROFILE_DEFAULTS):
            await check_agent_profiles(row)
        if not account_id and timezone_changed:
            await apply_schedules()
    return row


@router.patch("", response_model=SettingsResponse, response_model_by_alias=True)
async def update_settings(body: UpdateSettingsRequest) -> SettingsProfile:
    return await save_settings(body)


@router.patch("/personal", response_model=SettingsResponse, response_model_by_alias=True)
async def update_personal_settings(
    body: UpdateSettingsRequest, account: Account = Depends(current_account)
) -> SettingsProfile:
    return await save_settings(body, account.id)


@router.get("/apps", response_model=AppsSettingsResponse, response_model_by_alias=True)
async def get_app_settings(account: Account = Depends(current_account)) -> AppsSettingsResponse:
    settings = await SettingsProfile.get(account.id)
    projected = [await reads.get_app_settings(m, settings=settings) for m in iter_apps()]
    return AppsSettingsResponse(
        allowed_efforts=list(ALLOWED_EFFORTS),
        apps=[out for out in projected if out.agents or out.workflows or out.settings],
    )


@router.patch(
    "/apps",
    response_model=AppsSettingsResponse,
    response_model_by_alias=True,
)
async def update_app_settings(
    body: AppsSettingsUpdate, account: Account = Depends(current_session_account)
) -> AppsSettingsResponse:
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
    if body.agent_harnesses or body.agent_models or body.agent_billings:
        installation = await SettingsProfile.get()
        await check_agent_profiles(installation)
        profiles = await db_session().execute(
            select(SettingsProfile, Account.username).join(Account)
        )
        for settings, username in profiles:
            try:
                await check_agent_profiles(settings)
            except ProfileSettingsError as error:
                raise ProfileSettingsError(f"Personal profile for {username}: {error}") from error

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

    return await get_app_settings(account)

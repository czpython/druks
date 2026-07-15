from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Body, HTTPException

from druks.durable.engine import apply_schedules
from druks.extensions.loader import iter_extensions
from druks.extensions.registry import workflows
from druks.harnesses.exceptions import LoginError
from druks.harnesses.models import HarnessLogin
from druks.harnesses.registry import get_harnesses
from druks.notifications.models import Destination

from . import reads
from .datastructures import ALLOWED_EFFORTS
from .models import HarnessSettings, SettingsOverride, UserSettings
from .schemas import (
    ExtensionsSettingsResponse,
    ExtensionsSettingsUpdate,
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


def _resolve_harness(name: str) -> tuple[type, HarnessSettings]:
    harness = next((h for h in get_harnesses() if h.name == name), None)
    row = HarnessSettings.get(name)
    if harness is None or row is None:
        raise HTTPException(status_code=404, detail=f"Unknown harness: {name!r}")
    return harness, row


@router.get("/harnesses", response_model=list[HarnessResponse], response_model_by_alias=True)
async def list_harness_settings() -> list[HarnessResponse]:
    registered = {harness.name for harness in get_harnesses()}
    return [
        HarnessResponse.from_row(row, HarnessLogin.get_default(row.name))
        for row in HarnessSettings.all()
        if row.name in registered
    ]


@router.patch("/harnesses/{name}", response_model=HarnessResponse, response_model_by_alias=True)
async def update_harness_settings(name: str, body: HarnessUpdate) -> HarnessResponse:
    harness, row = _resolve_harness(name)
    updates = body.model_dump(exclude_unset=True, by_alias=False)
    if "model" in updates and not harness.has_model(updates["model"]):
        raise HTTPException(
            status_code=422,
            detail=f"{updates['model']!r} is not a {harness.name} model.",
        )
    _validate_effort(updates.get("effort"))
    _validate_timeout(updates.get("timeout"))
    if updates:
        row.update(**updates)
    return HarnessResponse.from_row(row, HarnessLogin.get_default(row.name))


@router.post("/harnesses/{name}/login/start")
async def start_harness_login(name: str) -> dict[str, str]:
    harness, _ = _resolve_harness(name)
    url, flow_id = await harness.login_start()
    return {"authorizeUrl": url, "flowId": flow_id}


@router.post(
    "/harnesses/{name}/login/complete",
    response_model=HarnessResponse,
    response_model_by_alias=True,
)
async def complete_harness_login(
    name: str,
    code: str = Body(..., embed=True),
    flow_id: str = Body(..., embed=True, alias="flowId"),
) -> HarnessResponse:
    # code = a bare code, a code#state pair, or the full redirect URL — the
    # harness parses whichever it is.
    harness, row = _resolve_harness(name)
    try:
        await harness.login_complete(flow_id=flow_id, pasted=code)
    except LoginError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return HarnessResponse.from_row(row, HarnessLogin.get_default(row.name))


@router.delete(
    "/harnesses/{name}/login", response_model=HarnessResponse, response_model_by_alias=True
)
async def disconnect_harness(name: str) -> HarnessResponse:
    harness, row = _resolve_harness(name)
    harness.disconnect()
    return HarnessResponse.from_row(row, HarnessLogin.get_default(row.name))


@router.get("", response_model=UserSettingsResponse, response_model_by_alias=True)
async def get_user_settings() -> UserSettings:
    return UserSettings.get()


@router.patch("", response_model=UserSettingsResponse, response_model_by_alias=True)
async def update_user_settings(
    body: UpdateUserSettingsRequest,
) -> UserSettings:
    row = UserSettings.get()
    if body.timezone is not None:
        tz = _validate_timezone(body.timezone)
        if tz != row.timezone:
            row.update_profile(timezone=tz)
            # Crons are evaluated in this timezone — repoint them now, not at
            # the next launch.
            apply_schedules()
    if "gate_park_destination_id" in body.model_fields_set:
        destination_id = body.gate_park_destination_id
        if destination_id and not Destination.get(destination_id):
            raise HTTPException(status_code=422, detail=f"Unknown destination {destination_id!r}")
        row.set_gate_park_destination(destination_id)
    return row


@router.get("/extensions", response_model=ExtensionsSettingsResponse, response_model_by_alias=True)
async def get_extension_settings() -> ExtensionsSettingsResponse:
    projected = (reads.get_extension_settings(m) for m in iter_extensions())
    return ExtensionsSettingsResponse(
        allowed_efforts=list(ALLOWED_EFFORTS),
        extensions=[out for out in projected if out.agents or out.workflows or out.settings],
    )


# An agent's model override is client data — reject a model no installed harness
# can run (nothing owns its namespace). A model new to a known namespace passes,
# so new models need no release.
def _validate_model(value: str | None) -> None:
    if value is not None and not any(harness.has_model(value) for harness in get_harnesses()):
        raise HTTPException(
            status_code=422,
            detail=f"No installed harness runs model {value!r}.",
        )


def _validate_effort(value: str | None) -> None:
    if value is not None and value not in ALLOWED_EFFORTS:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown effort {value!r}. Allowed: {list(ALLOWED_EFFORTS)}",
        )


def _validate_timeout(value: int | None) -> None:
    if value is not None and value <= 0:
        raise HTTPException(
            status_code=422,
            detail=f"Timeout must be a positive number of seconds, got {value!r}.",
        )


@router.patch(
    "/extensions", response_model=ExtensionsSettingsResponse, response_model_by_alias=True
)
async def update_extension_settings(body: ExtensionsSettingsUpdate) -> ExtensionsSettingsResponse:
    for name, model in body.agent_models.items():
        _validate_model(model)
        SettingsOverride.set_agent_model(name, model)

    for name, effort in body.agent_efforts.items():
        _validate_effort(effort)
        SettingsOverride.set_agent_effort(name, effort)

    for name, timeout in body.agent_timeouts.items():
        _validate_timeout(timeout)
        SettingsOverride.set_agent_timeout(name, timeout)

    try:
        for kind, changes in body.workflow_settings.items():
            workflow = workflows.get(kind)
            if not workflow:
                raise HTTPException(status_code=422, detail=f"Unknown workflow {kind!r}")
            for field, value in changes.items():
                workflow.override_setting(field, value)
        for extension_name, changes in body.extension_settings.items():
            extension = next((m for m in iter_extensions() if m.name == extension_name), None)
            if not extension:
                raise HTTPException(status_code=422, detail=f"Unknown extension {extension_name!r}")
            for field, value in changes.items():
                extension.override_setting(field, value)
    except ValueError as exc:
        # Domain rejections (unknown field, bad cron, failed constraint) → 422.
        # override_setting has already redacted any submitted value out of the
        # message, so this is safe to surface even for a rejected secret.
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    if any(
        field in ("schedule", "schedule_enabled")
        for changes in body.workflow_settings.values()
        for field in changes
    ):
        # Repoint the DBOS crons now, not at the next launch; the reconcile reads
        # the just-written overrides off this request's session.
        apply_schedules()

    return await get_extension_settings()

import contextlib
from datetime import datetime
from itertools import groupby
from operator import attrgetter
from zoneinfo import ZoneInfo

from croniter import CroniterBadDateError, croniter
from fastapi import APIRouter, HTTPException, Response
from sqlalchemy import case, func, select

from druks.api.dependencies import SessionDep
from druks.api.schemas import (
    DashboardOverview,
    DashboardRun,
    DashboardSchedule,
    DashboardSchedules,
    DashboardSection,
    ScheduledRun,
)
from druks.apps.loader import iter_apps
from druks.apps.registry import workflows
from druks.durable.dbos_state import latest_invocations
from druks.durable.engine import trigger_schedule
from druks.durable.enums import RunState
from druks.durable.exceptions import ScheduleUnavailable
from druks.durable.models import Artifact, Run
from druks.events.models import Event
from druks.settings import load_settings

PREVIEW_SIZE = 4
router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


@router.get("/overview", response_model=DashboardOverview)
async def get_overview(
    session: SessionDep, response: Response, app: str | None = None
) -> DashboardOverview:
    response.headers["Cache-Control"] = "no-store"
    apps = {owner.name: owner for owner in iter_apps()}
    if app is not None:
        if app not in apps:
            raise HTTPException(
                status_code=404, detail=f"Unknown app '{app}'. Select an installed app."
            )
        apps = {app: apps[app]}
    owners = {
        workflow.kind: owner.name for owner in apps.values() for workflow in owner.workflows()
    }
    current_runs = (
        Run.get_open_subjects(
            kinds=list(owners),
            states=(RunState.PARKED, RunState.RUNNING, RunState.FAILED, RunState.ORPHANED),
            include_subjectless=True,
        )
        .order_by(None)
        .subquery()
    )
    section = case(
        (current_runs.c.state == RunState.PARKED, "needs_you"),
        (current_runs.c.state == RunState.RUNNING, "running"),
        else_="failed",
    )
    # Both request columns are NULL outside needs_you, so running and failed
    # order by updated_at alone.
    request_time = case(
        (current_runs.c.state == RunState.PARKED, current_runs.c.input_requested_at)
    )
    request_id = case((current_runs.c.state == RunState.PARKED, current_runs.c.run_id))
    ranked_runs = (
        select(
            current_runs,
            section.label("section"),
            func.count().over(partition_by=section).label("total"),
            func.row_number()
            .over(
                partition_by=section,
                order_by=(
                    request_time.asc(),
                    request_id.asc(),
                    current_runs.c.updated_at.desc(),
                    current_runs.c.run_id.desc(),
                ),
            )
            .label("position"),
        )
        .where(
            (current_runs.c.state != RunState.PARKED)
            | (current_runs.c.input_requested_at.is_not(None) & (current_runs.c.presentation != ""))
        )
        .subquery()
    )
    previews = (
        select(
            ranked_runs.c.section,
            ranked_runs.c.total,
            ranked_runs.c.run_id.label("run"),
            ranked_runs.c.kind,
            ranked_runs.c.state,
            ranked_runs.c.subject_type,
            ranked_runs.c.subject_id,
            func.left(ranked_runs.c.subject_key, 240).label("subject_key"),
            ranked_runs.c.updated_at,
            ranked_runs.c.input_requested_at.label("parked_at"),
            ranked_runs.c.request_label,
            func.left(Artifact.title, 240).label("artifact_title"),
            ranked_runs.c.presentation,
            ranked_runs.c.request_url,
            func.left(ranked_runs.c.failure, 2048).label("failure"),
        )
        .outerjoin(Artifact, Artifact.agent_call_id == ranked_runs.c.latest_call_id)
        .where(ranked_runs.c.position <= PREVIEW_SIZE)
        .order_by(ranked_runs.c.section, ranked_runs.c.position)
    )
    history = select(
        func.max(Event.created_at)
        .filter(Event.type == "workflow.finished")
        .label("last_finished_at"),
        func.max(Event.created_at).filter(Event.type == "workflow.failed").label("last_failed_at"),
    ).where(Event.app.in_(list(apps)), Event.type.in_(("workflow.finished", "workflow.failed")))
    sections = {
        name: DashboardSection(total=0, rows=[]) for name in ("needs_you", "running", "failed")
    }
    for name, group in groupby((await session.execute(previews)).all(), key=attrgetter("section")):
        preview = list(group)
        sections[name] = DashboardSection(
            total=preview[0].total,
            rows=[
                DashboardRun.model_validate({**row._mapping, "app": owners[row.kind]})
                for row in preview
            ],
        )
    times = (await session.execute(history)).one()
    return DashboardOverview(
        **sections, last_finished_at=times.last_finished_at, last_failed_at=times.last_failed_at
    )


@router.get("/schedules", response_model=DashboardSchedules)
async def list_current_schedules(session: SessionDep, response: Response) -> DashboardSchedules:
    """Configured cadence and recorded invocations, not scheduler health."""
    response.headers["Cache-Control"] = "no-store"
    timezone = load_settings().timezone
    declared = [
        (owner.name, workflow)
        for owner in iter_apps()
        for workflow in owner.workflows()
        if workflow.every
    ]
    history = await session.execute(
        latest_invocations([workflow.kind for _, workflow in declared], limit=8)
    )
    runs = {
        name: [ScheduledRun.model_validate(row) for row in group]
        for name, group in groupby(history.all(), key=attrgetter("schedule_name"))
    }
    now = datetime.now(ZoneInfo(timezone))
    rows = []
    for owner, workflow in declared:
        cron = await workflow.get_schedule(session)
        is_enabled = await workflow.has_enabled_schedule(session)
        next_run_at = None
        if cron and is_enabled:
            with contextlib.suppress(CroniterBadDateError):
                next_run_at = croniter(cron, now, second_at_beginning=True).get_next(datetime)
        rows.append(
            DashboardSchedule(
                app=owner,
                kind=workflow.kind,
                cron=cron,
                default_cron=workflow.every,
                enabled=is_enabled,
                timezone=timezone,
                next_run_at=next_run_at,
                runs=runs.get(workflow.kind, []),
            )
        )
    return DashboardSchedules(rows=rows)


@router.post("/schedules/{kind}/run", status_code=202)
async def run_schedule(kind: str) -> dict[str, str]:
    workflow = workflows.get(kind)
    if not workflow or not workflow.every:
        raise HTTPException(404, "Schedule not found. Select a declared schedule.")
    try:
        run = await trigger_schedule(kind)
    except ScheduleUnavailable as error:
        raise HTTPException(503, str(error)) from error
    return {"run": run}

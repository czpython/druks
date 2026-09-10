from fastapi import APIRouter, HTTPException, Response
from sqlalchemy import case, func, select, true

from druks.api.schemas import (
    DashboardOverview,
    DashboardRun,
    DashboardSchedule,
    DashboardSchedules,
    DashboardSection,
    DashboardWork,
)
from druks.apps.loader import iter_apps
from druks.database import db_session
from druks.durable.enums import OPEN_STATES, RunState
from druks.durable.models import Artifact, Run
from druks.events.models import Event
from druks.settings import load_settings

PAGE_SIZE = 200
router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


@router.get("/overview", response_model=DashboardOverview)
async def get_overview(response: Response, app: str | None = None) -> DashboardOverview:
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
    current = (
        Run.get_open_subjects(
            kinds=list(owners),
            states=(RunState.PARKED, RunState.RUNNING, RunState.FAILED, RunState.ORPHANED),
            include_subjectless=True,
        )
        .order_by(None)
        .subquery()
    )
    section = case(
        (current.c.state == RunState.PARKED, "needs_you"),
        (current.c.state == RunState.RUNNING, "running"),
        else_="failed",
    )
    request_time = case((current.c.state == RunState.PARKED, current.c.input_requested_at))
    request_id = case((current.c.state == RunState.PARKED, current.c.run_id))
    ranked = (
        select(
            current,
            section.label("section"),
            func.count().over(partition_by=section).label("total"),
            func.row_number()
            .over(
                partition_by=section,
                order_by=(
                    request_time.asc(),
                    request_id.asc(),
                    current.c.updated_at.desc(),
                    current.c.run_id.desc(),
                ),
            )
            .label("position"),
        )
        .where(
            (current.c.state != RunState.PARKED)
            | (current.c.input_requested_at.is_not(None) & (current.c.presentation != ""))
        )
        .subquery()
    )
    previews = select(ranked).where(ranked.c.position <= 4).subquery()
    history = (
        select(
            func.max(Event.created_at)
            .filter(Event.type == "workflow.finished")
            .label("last_finished_at"),
            func.max(Event.created_at)
            .filter(Event.type == "workflow.failed")
            .label("last_failed_at"),
        )
        .where(Event.app.in_(apps), Event.type.in_(("workflow.finished", "workflow.failed")))
        .subquery()
    )
    statement = (
        select(
            history,
            previews.c.section,
            previews.c.total,
            previews.c.run_id.label("run"),
            previews.c.kind,
            previews.c.state,
            previews.c.subject_type,
            previews.c.subject_id,
            func.left(previews.c.subject_label, 240).label("subject_label"),
            previews.c.updated_at,
            previews.c.input_requested_at.label("parked_at"),
            previews.c.request_label,
            func.left(Artifact.title, 240).label("artifact_title"),
            previews.c.presentation,
            previews.c.request_url,
            func.left(previews.c.failure, 2048).label("failure"),
        )
        .select_from(history)
        .outerjoin(previews, true())
        .outerjoin(Artifact, Artifact.agent_call_id == previews.c.latest_call_id)
        .order_by(previews.c.section, previews.c.position)
    )
    rows = (await db_session().execute(statement)).all()
    sections = {
        name: DashboardSection(total=0, rows=[]) for name in ("needs_you", "running", "failed")
    }

    for row in rows:
        if row.run:
            sections[row.section].total = row.total
            sections[row.section].rows.append(
                DashboardRun.model_validate({**row._mapping, "app": owners[row.kind]})
            )
    return DashboardOverview(
        **sections,
        last_finished_at=rows[0].last_finished_at,
        last_failed_at=rows[0].last_failed_at,
    )


@router.get("/work", response_model=DashboardWork)
async def list_current_work(response: Response) -> DashboardWork:
    response.headers["Cache-Control"] = "no-store"
    owners = {workflow.kind: owner.name for owner in iter_apps() for workflow in owner.workflows()}
    current = (
        Run.get_open_subjects(
            kinds=list(owners),
            states=(*OPEN_STATES, RunState.ORPHANED),
            include_subjectless=True,
        )
        .order_by(None)
        .subquery()
    )
    statement = (
        select(
            current.c.run_id.label("run"),
            current.c.kind,
            current.c.state,
            current.c.subject_type,
            current.c.subject_id,
            func.left(current.c.subject_label, 240).label("subject_label"),
            current.c.updated_at,
            current.c.input_requested_at.label("parked_at"),
            current.c.request_label,
            func.left(Artifact.title, 240).label("artifact_title"),
            current.c.presentation,
            current.c.request_url,
            func.left(current.c.failure, 2048).label("failure"),
        )
        .outerjoin(Artifact, Artifact.agent_call_id == current.c.latest_call_id)
        .order_by(current.c.updated_at.desc(), current.c.run_id.desc())
        .limit(PAGE_SIZE + 1)
    )
    rows = (await db_session().execute(statement)).all()
    return DashboardWork(
        rows=[
            DashboardRun.model_validate({**row._mapping, "app": owners[row.kind]})
            for row in rows[:PAGE_SIZE]
        ],
        has_more=len(rows) > PAGE_SIZE,
    )


@router.get("/schedules", response_model=DashboardSchedules)
async def list_current_schedules(response: Response) -> DashboardSchedules:
    """Configured cadence, not scheduler health."""
    response.headers["Cache-Control"] = "no-store"
    timezone = load_settings().timezone
    return DashboardSchedules(
        rows=[
            DashboardSchedule(
                app=owner.name,
                kind=workflow.kind,
                cron=await workflow.get_schedule(),
                enabled=await workflow.has_enabled_schedule(),
                timezone=timezone,
            )
            for owner in iter_apps()
            for workflow in owner.workflows()
            if workflow.every
        ]
    )

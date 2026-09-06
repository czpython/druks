from fastapi import APIRouter, Response
from sqlalchemy import func, select

from druks.api.schemas import OverviewRun, OverviewSchedule, OverviewSchedules, OverviewWork
from druks.apps.loader import iter_apps
from druks.database import db_session
from druks.durable.enums import OPEN_STATES, RunState
from druks.durable.models import Run
from druks.user_settings.models import UserSettings

PAGE_SIZE = 200
router = APIRouter(prefix="/api/overview", tags=["overview"])


@router.get("/work", response_model=OverviewWork)
async def list_current_work(response: Response) -> OverviewWork:
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
            current.c.presentation,
            current.c.request_url,
            func.left(current.c.failure, 512).label("failure"),
        )
        .order_by(current.c.updated_at.desc(), current.c.run_id.desc())
        .limit(PAGE_SIZE + 1)
    )
    rows = (await db_session().execute(statement)).all()
    return OverviewWork(
        rows=[
            OverviewRun.model_validate({**row._mapping, "app": owners[row.kind]})
            for row in rows[:PAGE_SIZE]
        ],
        has_more=len(rows) > PAGE_SIZE,
    )


@router.get("/schedules", response_model=OverviewSchedules)
async def list_current_schedules(response: Response) -> OverviewSchedules:
    """Configured cadence, not scheduler health."""
    response.headers["Cache-Control"] = "no-store"
    timezone = (await UserSettings.get()).timezone
    return OverviewSchedules(
        rows=[
            OverviewSchedule(
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

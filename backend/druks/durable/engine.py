import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import datetime
from typing import TYPE_CHECKING, Any

from dbos import DBOS, DBOSConfig, Queue
from sqlalchemy.ext.asyncio import AsyncSession

from druks.database import create_async_engine_from_url, session_scope
from druks.db import db_session
from druks.durable.dbos_state import DBOS_SYSTEM_SCHEMA
from druks.durable.exceptions import ScheduleUnavailable
from druks.settings import load_settings
from druks.user_settings.models import InstallationSettings

if TYPE_CHECKING:
    from druks.workflows import Workflow

# Workflows enqueue here; execution distributes across whichever processes
# launched DBOS. One queue until a unit earns its own policy.
run_queue = Queue("druks")

_scheduled: list[tuple["type[Workflow]", Callable]] = []

_initialized = False
_engine = None


def _dbos_database_url(database_url: str) -> str:
    return database_url.replace("postgresql+psycopg://", "postgresql://", 1)


def init_dbos() -> None:
    """Construct the process DBOS singleton. Idempotent; call before app
    autodiscovery registers workflows, and before launch()."""
    global _initialized
    if _initialized:
        return
    settings = load_settings()
    # Both urls point at the app database: DBOS self-migrates its bookkeeping
    # into the dbos schema there, so derived Run.state is a same-DB read.
    url = _dbos_database_url(settings.database_url)
    config: DBOSConfig = {
        "name": "druks",
        "application_database_url": url,
        "system_database_url": url,
        "dbos_system_schema": DBOS_SYSTEM_SCHEMA,
        "log_level": settings.log_level,
        # One constant application version. DBOS recovers only the runs whose version
        # matches the process, and its default hashes workflow source: any edit to a
        # workflow would strand every parked run, because one host cannot drain them.
        "enable_patching": True,
    }
    DBOS(config=config)
    _initialized = True


def register_schedule(
    workflow: "type[Workflow]", run: Callable[[dict[str, Any]], Awaitable[Any]]
) -> None:
    # DBOS requires the scheduled time and context in this signature.
    @DBOS.workflow(name=f"{workflow.kind}.scheduled")
    async def scheduled_workflow(
        _scheduled_at: datetime, context: dict[str, Any] | None = None
    ) -> None:
        await run(context or {})

    _scheduled.append((workflow, scheduled_workflow))


async def apply_schedules(session: AsyncSession) -> None:
    declared = {workflow.kind for workflow, _ in _scheduled}
    existing = {row["schedule_name"]: row for row in await DBOS.list_schedules_async()}
    for name in existing.keys() - declared:
        await DBOS.delete_schedule_async(name)
    # Evaluate in the installation timezone so daily cadence follows DST.
    # Personal display preferences must not move a shared schedule.
    timezone = load_settings().timezone
    for workflow, entry in _scheduled:
        cron = await workflow.get_schedule(session)
        current = existing.get(workflow.kind)
        if not cron:
            await DBOS.delete_schedule_async(workflow.kind)
            continue
        is_enabled = await workflow.has_enabled_schedule(session)
        if current and not is_enabled:
            await asyncio.to_thread(DBOS.pause_schedule, workflow.kind)
        if not current or current["schedule"] != cron or current["cron_timezone"] != timezone:
            await DBOS.apply_schedules_async(
                [
                    {
                        "schedule_name": workflow.kind,
                        "workflow_fn": entry,
                        "schedule": cron,
                        "cron_timezone": timezone,
                    }
                ]
            )
        if is_enabled:
            await asyncio.to_thread(DBOS.resume_schedule, workflow.kind)
        elif not current:
            await asyncio.to_thread(DBOS.pause_schedule, workflow.kind)


async def trigger_schedule(kind: str) -> str:
    """Queue one invocation now and return its run. A paused schedule stays paused."""
    if not await DBOS.get_schedule_async(kind):
        raise ScheduleUnavailable(
            f"DBOS holds no schedule '{kind}'. Retry after the scheduler starts."
        )
    handle = await asyncio.to_thread(DBOS.trigger_schedule, kind)
    return handle.get_workflow_id()


async def launch() -> None:
    # Called with the serving loop running, so DBOS captures it as the main
    # loop and async steps share it.
    DBOS.launch()
    async with session_scope(_step_engine()) as session:
        # Commit the singleton before concurrent settings requests can create it.
        await InstallationSettings.get_or_create(session)
        await apply_schedules(session)


def shutdown() -> None:
    # No-op when this process never launched DBOS — a test that runs the app
    # lifespan with app.state.settings pre-set skips the branch that launches it
    # — so the lifespan can call shutdown() unconditionally.
    global _initialized
    if _initialized:
        DBOS.destroy()
        _initialized = False


def configure_engine(engine) -> None:
    global _engine
    _engine = engine


def _step_engine():
    global _engine
    if not _engine:
        _engine = create_async_engine_from_url(load_settings().database_url)
    return _engine


def step_session() -> AbstractAsyncContextManager[AsyncSession]:
    # One transaction per durable step (the body itself does no IO).
    return session_scope(_step_engine())


@asynccontextmanager
async def bound_session() -> AsyncIterator[AsyncSession]:
    # The session this task already holds, else a step's own for the block: a
    # body starts a child run outside any step and holds none.
    if db_session.registry.has():
        yield db_session()
        return
    async with step_session() as session:
        yield session

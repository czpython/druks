import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import datetime
from typing import TYPE_CHECKING, Any

from dbos import DBOS, DBOSConfig, Queue
from sqlalchemy.ext.asyncio import AsyncSession

from druks.database import create_async_engine_from_url, session_scope
from druks.db import db_session
from druks.durable.dbos_state import DBOS_SYSTEM_SCHEMA
from druks.settings import load_settings
from druks.user_settings.models import InstallationSettings

if TYPE_CHECKING:
    from druks.workflows import Workflow

logger = logging.getLogger(__name__)

# Workflows enqueue here; execution distributes across whichever processes
# launched DBOS. One queue until a unit earns its own policy.
run_queue = Queue("druks")

# (workflow class, entry fn) recorded by register_schedule(); turned into DBOS
# schedules at launch(), each at the class's resolved cadence.
_scheduled: list[tuple["type[Workflow]", Callable]] = []

_initialized = False
_engine = None


def _dbos_database_url(database_url: str) -> str:
    # DBOS drives its own engine off a bare postgresql:// URL.
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
    }
    DBOS(config=config)
    _initialized = True


def register_schedule(
    cls: "type[Workflow]", run: Callable[[dict[str, Any]], Awaitable[Any]]
) -> None:
    # The scheduled entry must satisfy DBOS's ScheduledWorkflow signature exactly
    # — DBOS invokes it as fn(scheduled_at, context) — or the cron silently never
    # fires. A cron carries no subject (a framework run), so run() gets no kwargs.
    @DBOS.workflow(name=f"{cls.kind}.scheduled")
    async def _sched_entry(_scheduled_at: datetime, context: dict[str, Any] | None = None) -> None:
        await run(context or {})

    _scheduled.append((cls, _sched_entry))


async def apply_schedules(session: AsyncSession) -> None:
    # Declared crons name the schedule set; the operator's settings overrides only
    # retune or pause a declared name, never add one — so an undeclared sys-db
    # schedule is a renamed/removed cron: drop it. The workflow class owns its
    # resolved knobs (get_schedule reads the override off the ambient session:
    # launch() binds one, and the settings route that just wrote an override
    # re-runs this on its request session).
    declared = {cls.kind for cls, _ in _scheduled}
    for existing in await DBOS.list_schedules_async():
        if existing["schedule_name"] not in declared:
            await DBOS.delete_schedule_async(existing["schedule_name"])
    # Evaluate in the installation timezone so daily cadence follows DST.
    # Personal display preferences must not move a shared schedule.
    timezone = load_settings().timezone
    for cls, fn in _scheduled:
        await DBOS.delete_schedule_async(cls.kind)
        cron = await cls.get_schedule(session)
        if await cls.has_enabled_schedule(session) and cron:
            await DBOS.create_schedule_async(
                schedule_name=cls.kind, workflow_fn=fn, schedule=cron, cron_timezone=timezone
            )


async def launch() -> None:
    # Called with the serving loop running, so DBOS captures it as the main
    # loop and async steps share it.
    DBOS.launch()
    async with session_scope(_step_engine()) as session:
        # Commit the singleton before concurrent settings requests can create it.
        await InstallationSettings.get(session)
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

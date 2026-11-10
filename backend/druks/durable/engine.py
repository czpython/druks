import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import datetime
from functools import cache
from typing import TYPE_CHECKING, Any

from dbos import DBOS, DBOSClient, DBOSConfig, EnqueueOptions
from dbos._error import DBOSQueueDeduplicatedError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from druks.database import create_async_engine_from_url, session_scope
from druks.db import AutocommitSession, db_session
from druks.durable.constants import NOTIFICATIONS_QUEUE, PAUSE_QUEUE, RUN_QUEUE, TASK_QUEUE
from druks.durable.dbos_state import DBOS_SYSTEM_SCHEMA, workflow_status
from druks.durable.exceptions import FatalError, ScheduleUnavailable
from druks.harnesses.exceptions import HarnessError
from druks.settings import get_settings
from druks.user_settings.models import InstallationSettings

if TYPE_CHECKING:
    from druks.workflows import Workflow

_QUEUES = (RUN_QUEUE, TASK_QUEUE, PAUSE_QUEUE, NOTIFICATIONS_QUEUE)

_workflow_schedules: list[tuple["type[Workflow]", Callable]] = []
_task_schedules: list[tuple[str, str, Callable]] = []

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
    settings = get_settings()
    # System URL is the app database: DBOS self-migrates its bookkeeping into
    # the dbos schema there, so derived Run.state is a same-DB read.
    url = _dbos_database_url(settings.database_url.get_secret_value())
    config: DBOSConfig = {
        "name": "druks",
        "system_database_url": url,
        "sys_db_pool_size": settings.dbos_pool_size,
        "dbos_system_schema": DBOS_SYSTEM_SCHEMA,
        "log_level": settings.log_level,
        # One constant application version. DBOS recovers only the runs whose version
        # matches the process, and its default hashes workflow source: any edit to a
        # workflow would strand every parked run, because one host cannot drain them.
        "enable_patching": True,
    }
    # DBOS logs every exception that leaves a workflow, with no opt-out;
    # _execute_run already records a classified failure on the run.
    logging.getLogger("dbos").addFilter(
        lambda record: (
            not (
                record.exc_info
                # ast-grep-ignore: rule-3
                and isinstance(record.exc_info[1], (FatalError, HarnessError))
            )
        )
    )
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

    _workflow_schedules.append((workflow, scheduled_workflow))


def register_task_schedule(name: str, cron: str, entry: Callable) -> None:
    _task_schedules.append((name, cron, entry))


async def apply_schedules(session: AsyncSession) -> None:
    declared = {workflow.kind for workflow, _ in _workflow_schedules} | {
        name for name, _, _ in _task_schedules
    }
    existing = {row["schedule_name"]: row for row in await DBOS.list_schedules_async()}
    for name in existing.keys() - declared:
        await DBOS.delete_schedule_async(name)
    # Evaluate in the installation timezone so daily cadence follows DST.
    # Personal display preferences must not move a shared schedule.
    timezone = get_settings().timezone
    for workflow, entry in _workflow_schedules:
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
    # A task's every= is a fixed UTC cadence the code owns, not an operator's.
    for name, cron, entry in _task_schedules:
        current = existing.get(name)
        if not current or current["schedule"] != cron or current["cron_timezone"] != "UTC":
            await DBOS.apply_schedules_async(
                [
                    {
                        "schedule_name": name,
                        "workflow_fn": entry,
                        "schedule": cron,
                        "cron_timezone": "UTC",
                    }
                ]
            )


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
    # loop and async steps share it. Queues persist in the system database and
    # must be registered after launch.
    DBOS.launch()
    for name in _QUEUES:
        await DBOS.register_queue_async(name)
    async with session_scope(_step_engine()) as session:
        # Commit the singleton before concurrent settings requests can create it.
        await InstallationSettings.get_or_create(session)
        await apply_schedules(session)


def shutdown() -> None:
    # No-op when this process never launched DBOS — a test that runs the app
    # lifespan with app.state.engine pre-set skips the branch that launches it
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
        _engine = create_async_engine_from_url(get_settings().database_url.get_secret_value())
    return _engine


def step_session() -> AbstractAsyncContextManager[AsyncSession]:
    # One transaction for the block: core's own short units of row work.
    return session_scope(_step_engine())


def autocommit_session() -> AbstractAsyncContextManager[AsyncSession]:
    # Author code in a step and on the box: each operation commits on its own, so
    # the code holds no connection between operations; atomic() groups writes.
    return session_scope(_step_engine(), AutocommitSession)


@cache
def get_dbos_client() -> DBOSClient:
    """The client that writes to DBOS inside a caller's transaction."""
    url = _dbos_database_url(get_settings().database_url.get_secret_value())
    return DBOSClient(system_database_url=url, dbos_system_schema=DBOS_SYSTEM_SCHEMA, lazy=True)


async def enqueue_in_transaction(session: AsyncSession, options: EnqueueOptions, *args: Any) -> str:
    """Enqueue a workflow in the session's transaction: it enters the queue when the
    transaction commits, and a rollback drops it. Returns the workflow that holds the
    deduplication slot: this one, or the live one."""
    while True:
        try:
            # The savepoint drops a held slot's collision; the caller's transaction goes on.
            async with session.begin_nested():
                await session.run_sync(
                    lambda sync: get_dbos_client().enqueue_in_transaction(sync, options, *args)
                )
            return options["workflow_id"]
        except DBOSQueueDeduplicatedError:
            holder = await session.scalar(
                select(workflow_status.c.workflow_uuid).where(
                    workflow_status.c.queue_name == options["queue_name"],
                    workflow_status.c.deduplication_id == options["deduplication_id"],
                )
            )
            if holder:
                return holder
            # The holder ended between the collision and the read, so the slot is free.


@asynccontextmanager
async def bound_session() -> AsyncIterator[AsyncSession]:
    # The session this task already holds, else a step's own for the block: a
    # body starts a child run outside any step and holds none.
    if db_session.registry.has():
        yield db_session()
        return
    async with step_session() as session:
        yield session

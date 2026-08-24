import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import datetime
from typing import TYPE_CHECKING, Any

from dbos import DBOS, DBOSConfig, Queue
from sqlalchemy.orm import Session

from druks.database import create_engine_from_url, db_session, get_session, session_scope
from druks.durable.dbos_state import DBOS_SYSTEM_SCHEMA
from druks.settings import load_settings
from druks.user_settings.models import UserSettings

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


def apply_schedules() -> None:
    # Declared crons name the schedule set; the operator's settings overrides only
    # retune or pause a declared name, never add one — so an undeclared sys-db
    # schedule is a renamed/removed cron: drop it. The workflow class owns its
    # resolved knobs (get_schedule reads the override off the ambient session:
    # launch() binds one, and the settings route that just wrote an override
    # re-runs this on its request session).
    declared = {cls.kind for cls, _ in _scheduled}
    for existing in DBOS.list_schedules():
        if existing["schedule_name"] not in declared:
            DBOS.delete_schedule(existing["schedule_name"])
    # Crons fire on the operator's clock: "daily at midnight" means their
    # midnight. Evaluating in-zone (rather than converting to a UTC cron once)
    # keeps wall-clock cadences honest across DST. The timezone setting is
    # validated at its write boundary, so it's a real IANA name here.
    timezone = UserSettings.get().timezone
    for cls, fn in _scheduled:
        DBOS.delete_schedule(cls.kind)
        cron = cls.get_schedule()
        if cls.has_enabled_schedule() and cron:
            DBOS.create_schedule(
                schedule_name=cls.kind, workflow_fn=fn, schedule=cron, cron_timezone=timezone
            )


def launch() -> None:
    DBOS.launch()
    with session_scope(_step_engine()):
        apply_schedules()


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
        _engine = create_engine_from_url(load_settings().database_url)
    return _engine


@asynccontextmanager
async def step_session() -> AsyncIterator[Session]:
    # One transaction per durable step (the body itself does no IO).
    session = get_session(_step_engine())
    db_session.registry.set(session)
    try:
        yield session
    except BaseException:
        session.rollback()
        raise
    else:
        session.commit()
    finally:
        db_session.remove()
        session.close()

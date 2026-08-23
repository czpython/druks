import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, scoped_session, sessionmaker

_ALEMBIC_INI = Path(__file__).resolve().parent.parent / "alembic.ini"


def run_migrations(database_url: str) -> None:
    """Bring the schema to head — the migrate container's job (``druks
    init-db``). Core first, then each installed app's own migrations: an
    external app owns an independent history, so this order is the contract,
    not a cross-repo revision link. Production schema is owned by Alembic."""
    from alembic import command
    from alembic.config import Config

    core = Config(str(_ALEMBIC_INI))
    core.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(core, "head")
    for name, migrations_dir in _app_migration_dirs():
        # The app runs through the platform env (the shared ``alembic.ini``
        # script_location); only its own revisions (version_locations) and version
        # table belong to the app.
        app_config = Config(str(_ALEMBIC_INI))
        app_config.set_main_option("version_locations", str(migrations_dir / "versions"))
        app_config.set_main_option("sqlalchemy.url", database_url)
        # Own version table per history, else the app reads core's head from the
        # shared default ``alembic_version`` and can't locate it in its own scripts.
        app_config.attributes["version_table"] = f"alembic_version_{name}"
        command.upgrade(app_config, "head")


def make_app_migration(app_name: str, message: str, database_url: str) -> None:
    """Autogenerate a revision for one installed app into its own ``versions/``,
    diffing the app's prefix-scoped tables against the live DB. The dev DB must be
    at the app's head first (``druks init-db``) — Alembic diffs models against the
    database, not migration state."""
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import MetaData

    from druks.apps.loader import get_app, import_app_models
    from druks.models import Base

    import_app_models()
    app = get_app(app_name)
    package_dir = app.package_dir()
    if not package_dir:
        raise ValueError(f"app {app_name!r} ships no package to write migrations into")
    migrations_dir = package_dir / "migrations"

    scoped = MetaData()
    for table in Base.metadata.tables.values():
        if table.name.startswith(app.table_prefix):
            table.to_metadata(scoped)

    config = Config(str(_ALEMBIC_INI))
    config.set_main_option("version_locations", str(migrations_dir / "versions"))
    config.set_main_option("sqlalchemy.url", database_url)
    config.attributes["version_table"] = f"alembic_version_{app.name}"
    config.attributes["target_metadata"] = scoped
    command.revision(config, message=message, autogenerate=True)


def _app_migration_dirs() -> list[tuple[str, Path]]:
    from druks.apps.loader import iter_apps

    found: list[tuple[str, Path]] = []
    for app in iter_apps():
        package_dir = app.package_dir()
        if package_dir and (package_dir / "migrations" / "versions").is_dir():
            found.append((app.name, package_dir / "migrations"))
    return found


def create_engine_from_url(database_url: str):
    # Normal transactional engine: one transaction per request/task, committed
    # at the lifecycle boundary (the API session dependency, the worker session
    # wrapper) so a failed unit of work rolls back instead of leaving partial
    # writes. Model methods ``flush()``; the boundary commits. Low pool_timeout
    # because a checkout wait blocks the event loop. The pool serves every
    # concurrent run's steps plus request handling at once: a modest steady
    # pool, with overflow doing the burst work — overflow connections open on
    # demand and close on return, so the ceiling is high while idle cost is
    # not. Ceiling 50 keeps the appliance (with DBOS's two engines at 20 each)
    # inside Postgres's default 100 connections.
    return create_engine(
        database_url,
        pool_pre_ping=True,
        pool_timeout=5,
        pool_size=20,
        max_overflow=30,
    )


def get_session(engine) -> Session:
    return Session(engine, autocommit=False, autoflush=True)


def _session_scope() -> object | None:
    try:
        return asyncio.current_task()
    except RuntimeError:
        return None


_session_factory = sessionmaker(class_=Session, autoflush=True)
db_session: scoped_session = scoped_session(_session_factory, scopefunc=_session_scope)


def configure_session(engine) -> None:
    _session_factory.configure(bind=engine)


@contextmanager
def session_scope(engine) -> Iterator[None]:
    """Bind a fresh DB session to the ``db_session`` registry for the block,
    removing it on exit — for work that runs outside the request/task session
    boundary (launch's schedule reconcile, a stream's per-poll snapshot), so it
    can't leak a session per viewer. Commits on success like the request
    boundary — a bare Session close rolls back, silently discarding the
    block's writes."""
    with get_session(engine) as session:
        db_session.registry.set(session)
        try:
            yield
        except BaseException:
            session.rollback()
            raise
        else:
            session.commit()
        finally:
            db_session.remove()

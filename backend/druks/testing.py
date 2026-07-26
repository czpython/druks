import base64
import os
import secrets
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from unittest import mock

import pytest
import redis.asyncio as aioredis
from dbos import run_dbos_database_migrations
from fastapi.testclient import TestClient
from sqlalchemy import text, update
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from uuid_utils import uuid7

import druks.durable.models  # noqa: F401
import druks.harnesses.models  # noqa: F401
import druks.mcp.models  # noqa: F401
import druks.notifications.models  # noqa: F401
import druks.redis
import druks.skills.models  # noqa: F401
import druks.user_settings.models  # noqa: F401
from druks.bootstrap import seed
from druks.database import _session_factory, configure_session, create_engine_from_url, db_session
from druks.durable import AgentCall, Run
from druks.durable.dbos_state import DBOS_SYSTEM_SCHEMA, workflow_status
from druks.durable.engine import _dbos_database_url, configure_engine
from druks.extensions.loader import import_extension_models, iter_extensions
from druks.models import Base, StoredSubject
from druks.settings import Settings
from druks.workflows import Workflow, WorkflowError, _bind_instance, current_workflow

# App commits become savepoints inside the fixture's outer transaction, which
# remains available for rollback at test teardown.
_session_factory.configure(join_transaction_mode="create_savepoint")
os.environ.setdefault("DRUKS_SECRETS_KEY", base64.b64encode(secrets.token_bytes(32)).decode())
# Workflow classes resolve their extension from package ownership at definition time.
iter_extensions()

# druks.testing is the author door for testing an extension: these four, plus the
# fixtures the pytest11 entry point registers. Everything else here is the
# repository suite's own harness.
__all__ = [
    "init_db",
    "run_workflow",
    "seed_call",
    "seed_run",
]

# The fixtures never read the application's settings: DRUKS_DATABASE_URL and
# DRUKS_REDIS_URL name a live instance, and these fixtures create tables and run
# FLUSHDB. They carry their own keys, defaulting to a database and a Redis index
# nothing else uses.
TEST_DATABASE_URL = os.environ.get(
    "DRUKS_TEST_DATABASE_URL", "postgresql+psycopg://druks:druks@localhost:5432/druks_test"
)
TEST_REDIS_URL = os.environ.get("DRUKS_TEST_REDIS_URL", "redis://127.0.0.1:6379/15")

# Under pytest, druks IS the test instance. Settings are read from the environment
# wherever they're needed — the app's Redis dialer among them — so overriding here is
# what keeps the code under test on the same database and Redis the fixtures own.
os.environ["DRUKS_DATABASE_URL"] = TEST_DATABASE_URL
os.environ["DRUKS_REDIS_URL"] = TEST_REDIS_URL


@pytest.fixture(scope="session")
def _druks_engine() -> Iterator[Engine]:
    engine = create_engine_from_url(TEST_DATABASE_URL)
    yield engine
    engine.dispose()


def init_db(engine: Engine) -> None:
    """Create every installed extension's tables and the first-start seed rows.
    Idempotent; production owns its schema through Alembic instead."""
    import_extension_models()
    # citext backs the case-insensitive email columns and must exist before create_all.
    with engine.begin() as connection:
        connection.execute(text("CREATE EXTENSION IF NOT EXISTS citext"))
    Base.metadata.create_all(engine)
    seed(engine)


@pytest.fixture(scope="session")
def _druks_schema(_druks_engine: Engine) -> Iterator[None]:
    init_db(_druks_engine)
    # DBOS's internal metadata retains an unresolved schema placeholder outside
    # its migration path, so tests build the system tables through its migrations.
    run_dbos_database_migrations(
        _dbos_database_url(_druks_engine.url.render_as_string(hide_password=False)),
        schema=DBOS_SYSTEM_SCHEMA,
    )
    yield


@pytest.fixture
def druks_db(_druks_engine: Engine, _druks_schema: None) -> Iterator[Session]:
    connection = _druks_engine.connect()
    transaction = connection.begin()
    # The app lifespan must not dispose the connection before this fixture rolls it back.
    connection.dispose = lambda: None  # type: ignore[method-assign]
    configure_session(connection)
    configure_engine(connection)
    db_session.remove()
    session = Session(
        connection,
        join_transaction_mode="create_savepoint",
        autoflush=True,
    )
    db_session.registry.set(session)
    try:
        yield session
    finally:
        db_session.remove()
        configure_engine(None)
        transaction.rollback()
        connection.close()


async def _operator_account():
    from druks.accounts.context import current_account_id
    from druks.accounts.models import Account

    account = Account.get_or_create("op@example.com")
    current_account_id.set(account.id)
    return account


def make_settings(tmp_path: Path, **overrides: object) -> Settings:
    defaults = {
        "data_dir": tmp_path,
        "database_url": TEST_DATABASE_URL,
        "webhook_secret": "test-secret",
        "github_api_url": "https://api.github.com",
        "github_operator_app_id": None,
        "github_operator_private_key_path": None,
        "github_reviewer_app_id": None,
        "github_reviewer_private_key_path": None,
        "linear_webhook_secret": "",
        "linear_api_key": None,
        # Null jira explicitly so a developer's real JIRA_* env vars don't leak
        # in and make "unconfigured" tests see a configured tracker.
        "jira_base_url": None,
        "jira_email": None,
        "jira_api_token": None,
        "redis_url": TEST_REDIS_URL,
        "log_level": "WARNING",
    }
    defaults.update(overrides)
    return Settings(**defaults)  # type: ignore[arg-type]


def configure_app_for_test(
    *,
    settings: Settings,
    engine: object | None = None,
    authenticated: bool = True,
):
    from druks.accounts.dependencies import current_account, current_session_account
    from druks.api.app import app

    if not engine:
        engine = db_session().get_bind()
    configure_session(engine)
    app.state.settings = settings
    app.state.engine = engine
    if authenticated:
        app.dependency_overrides[current_account] = _operator_account
        app.dependency_overrides[current_session_account] = _operator_account
    return app


@pytest.fixture
def druks_client(druks_db: Session, tmp_path: Path) -> Iterator[TestClient]:
    from druks.accounts.dependencies import current_account, current_session_account

    dependencies = (current_account, current_session_account)
    app = configure_app_for_test(
        settings=make_settings(tmp_path),
        engine=druks_db.connection(),
    )
    # The app lifespan opens the Redis client on its own event loop and closes it
    # there; one left behind by another loop cannot be closed from it. Drop it on
    # both sides so each consumer dials on the loop it runs on.
    druks.redis._client = None
    try:
        with TestClient(app) as client:
            yield client
    finally:
        druks.redis._client = None
        for dependency in dependencies:
            if app.dependency_overrides.get(dependency) is _operator_account:
                del app.dependency_overrides[dependency]


@pytest.fixture
async def druks_redis() -> AsyncIterator[None]:
    # Bind the test Redis directly rather than through get_client(), which dials
    # whatever the application is configured to use. Flush on a fresh client and
    # close it again, so the test body starts with none bound and every consumer
    # dials on the loop it runs on — the previous test's loop is gone.
    druks.redis._client = aioredis.from_url(TEST_REDIS_URL)
    await druks.redis._client.flushdb()
    await druks.redis.close_client()
    yield


@pytest.fixture
def druks_without_dispatch() -> Iterator[None]:
    """Workflow starts and run-phase writes become no-ops, so a test that stands up no
    durable engine still exercises the paths that reach for one. A cancel writes the
    terminal status DBOS would, since a Run's state derives from it."""

    async def _noop(*args, **kwargs):
        return ""

    async def _phase_noop(*args, **kwargs):
        pass

    async def _cancel(workflow_id: str) -> None:
        db_session().execute(
            update(workflow_status)
            .where(workflow_status.c.workflow_uuid == workflow_id)
            .values(status="CANCELLED")
        )

    with (
        mock.patch.object(Workflow, "start", classmethod(_noop)),
        mock.patch("druks.agents.set_run_phase", _phase_noop),
        mock.patch("dbos.DBOS.cancel_workflow_async", _cancel),
    ):
        yield


@pytest.fixture
def druks_without_remote_config(monkeypatch) -> None:
    """Every ``.druks`` namespace lookup misses, so prompts resolve to their bundled
    templates and extension config to its declared defaults — no repo credentials."""

    async def _missing(**_kwargs):
        return None

    monkeypatch.setattr("druks.prompts.resolver.fetch_file", _missing)
    monkeypatch.setattr("druks.extensions.config.fetch_file", _missing)


async def run_workflow(workflow_class, *, subject: StoredSubject | None = None, **input) -> object:
    """Run a workflow's body against ``subject`` and return its result, without a
    durable engine — no checkpoints, no lifecycle events, no retries. Only the
    single-operation ``run()`` shape: a ``run_multistep`` body checkpoints each
    ``@step`` through the engine, which isn't here."""
    if workflow_class._body_method != "run":
        raise WorkflowError(
            f"{workflow_class.__name__} orchestrates steps, and every @step checkpoints "
            "through the durable engine — run_workflow has none. Test its steps directly, "
            "or exercise the whole workflow against a live engine."
        )
    identity = None
    if subject:
        identity = subject.identity
    instance, run_kwargs = _bind_instance(workflow_class, identity, input)
    body = getattr(workflow_class, workflow_class._body_method)
    token = current_workflow.set(instance)
    try:
        return await body.__wrapped__(instance, **run_kwargs)
    finally:
        current_workflow.reset(token)


def seed_run(
    session: Session,
    *,
    kind: str,
    subject: StoredSubject | None = None,
    state: str = "running",
    run_id: str | None = None,
    input_gate: str | None = None,
    input_request: dict | None = None,
    failure: str | None = None,
    account_id: str = "system",
) -> Run:
    if state == "parked" and not input_gate:
        raise ValueError("input_gate is required for a parked run")
    run = Run(
        id=run_id or str(uuid7()),
        kind=kind,
        input_gate=input_gate,
        input_request=input_request,
        failure=failure,
        account_id=account_id,
    )
    session.add(run)
    session.flush()
    identity = None
    if subject:
        identity = subject.identity
    seed_dbos_status(session, run.id, state, subject=identity)
    session.expire(run, ["state", "updated_at"])
    return run


def seed_dbos_status(
    session: Session,
    run_id: str,
    state: str,
    *,
    subject: dict | None = None,
) -> None:
    """Write the ``dbos.workflow_status`` row a Run's ``state`` derives from. There is
    no state column, so a run seeded without this one reads as though it never ran."""
    status = {
        "scheduled": "ENQUEUED",
        "running": "PENDING",
        "parked": "PENDING",
        "finished": "SUCCESS",
        "failed": "ERROR",
        "cancelled": "CANCELLED",
    }[state]
    attributes = None
    if subject:
        attributes = {"subject_type": subject["type"], "subject_id": str(subject["id"])}
    session.execute(
        workflow_status.insert().values(
            workflow_uuid=run_id,
            status=status,
            updated_at=int(Base.utc_now().timestamp() * 1000),
            attributes=attributes,
        )
    )
    session.flush()


def seed_call(
    session: Session,
    run: Run,
    agent: str,
    *,
    status: str = "succeeded",
    model: str | None = "gpt-5.5",
    last_error: str | None = None,
) -> AgentCall:
    """An agent call on a run, stamped with the id of the agent that made it."""
    call = AgentCall(
        run_id=run.id,
        agent=agent,
        model=model,
        status=status,
        last_error=last_error,
        finished_at=Base.utc_now() if status != "running" else None,
        sandbox_host_id=f"test-host-{run.id}",
    )
    session.add(call)
    session.flush()
    return call

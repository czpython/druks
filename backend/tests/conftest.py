import base64
import os
import secrets
from pathlib import Path
from unittest import mock

import druks.redis
import pytest
from druks.database import (
    _session_factory,
    configure_session,
    create_engine_from_url,
    init_db,
)
from druks.extensions.loader import iter_extensions, register_workflow_package
from druks.settings import Settings
from sqlalchemy.orm import Session

# Every Session the suite opens binds to the per-test connection (the _txn
# fixture), which already has a transaction open; create_savepoint turns the
# app's own commits into savepoints so they don't end that transaction — the
# whole test rolls back at teardown, on the SAME real-transaction engine
# production uses (no AUTOCOMMIT pretence). On the durable tests, whose factory
# binds to a plain engine with no open transaction, the mode is inert.
_session_factory.configure(join_transaction_mode="create_savepoint")

# Stored secrets encrypt at rest; the suite needs a key before any model write
# or delivery read touches them. setdefault so an operator-exported key (or a
# rotation test's monkeypatch) still wins.
os.environ.setdefault("DRUKS_SECRETS_KEY", base64.b64encode(secrets.token_bytes(32)).decode())

# A Workflow class resolves its declaring extension at definition time, from
# packages the loader registers before importing. Tests import workflow modules
# directly and some declare their own workflows, so both register here — before
# collection imports any test module.
iter_extensions()
for test_module in ("test_durable_sdk", "test_notifications_durable"):
    register_workflow_package(test_module, None)


class FakeRedis:
    # The subset the run lock and the MCP OAuth cache use; one instance per
    # suite, keys per test are distinct enough (run ids, server names) that
    # cross-test bleed can't collide.
    def __init__(self) -> None:
        self._data: dict[str, bytes] = {}
        # TTLs are recorded, never enforced — enough to observe a refresh.
        self._ttls: dict[str, int] = {}
        self._zsets: dict[str, dict[str, float]] = {}

    async def set(self, key: str, value: str, *, nx: bool = False, ex: int | None = None):
        if nx and key in self._data:
            return None
        self._data[key] = value.encode()
        if ex is not None:
            self._ttls[key] = ex
        return True

    async def get(self, key: str) -> bytes | None:
        return self._data.get(key)

    async def getex(self, key: str, *, ex: int | None = None) -> bytes | None:
        value = self._data.get(key)
        if value is not None and ex is not None:
            self._ttls[key] = ex
        return value

    async def exists(self, key: str) -> int:
        return int(key in self._data)

    async def getdel(self, key: str) -> bytes | None:
        self._ttls.pop(key, None)
        return self._data.pop(key, None)

    # Sorted sets — the per-login gate's active-user registry. Stored apart
    # from the string keys; scores kept for the range prune.
    async def zadd(self, key: str, mapping: dict[str, float]) -> int:
        zset = self._zsets.setdefault(key, {})
        added = sum(1 for member in mapping if member not in zset)
        zset.update(mapping)
        return added

    async def zrem(self, key: str, *members: str) -> int:
        zset = self._zsets.get(key, {})
        return sum(1 for member in members if zset.pop(member, None) is not None)

    async def zcard(self, key: str) -> int:
        return len(self._zsets.get(key, {}))

    async def zremrangebyscore(self, key: str, low: object, high: float) -> int:
        zset = self._zsets.get(key, {})
        floor = float("-inf") if low in ("-inf", None) else float(low)  # type: ignore[arg-type]
        doomed = [member for member, score in zset.items() if floor <= score <= float(high)]
        for member in doomed:
            del zset[member]
        return len(doomed)

    async def delete(self, key: str) -> None:
        self._data.pop(key, None)
        self._ttls.pop(key, None)

    async def expire(self, key: str, seconds: int) -> bool:
        if key not in self._data:
            return False
        self._ttls[key] = seconds
        return True

    async def aclose(self) -> None:
        return


_fake_redis = FakeRedis()
druks.redis._client = _fake_redis  # get_client() returns it instead of connecting


async def _keep_fake_client() -> None:
    # The app lifespan's shutdown would null _client, and the next get_client()
    # would dial a real Redis; the suite keeps the fake for its whole life.
    return


druks.redis.close_client = _keep_fake_client


@pytest.fixture(autouse=True)
def _no_durable_dispatch(request):
    # Routes start durable work and agent runs push/read a DBOS phase event;
    # tests that don't stand up DBOS get a no-op so those calls don't reach an
    # engine that isn't there. The *_durable tests run the real engine.
    if "durable" in request.module.__name__:
        yield
        return

    from druks.workflows import Workflow

    async def _noop(*args, **kwargs):
        return ""

    async def _phase_noop(*args, **kwargs):
        pass

    async def _dbos_cancel(workflow_id: str) -> None:
        # DBOS's half of Run.cancel(): without a launched engine the real call
        # raises, and derived state needs the terminal status it would write.
        from druks.database import db_session
        from druks.durable.dbos_state import workflow_status
        from sqlalchemy import update

        db_session().execute(
            update(workflow_status)
            .where(workflow_status.c.workflow_uuid == workflow_id)
            .values(status="CANCELLED")
        )

    with (
        mock.patch.object(Workflow, "start", classmethod(_noop)),
        mock.patch("druks.agents.set_run_phase", _phase_noop),
        mock.patch("druks.build.extension.get_run_phase", _phase_noop),
        mock.patch("dbos.DBOS.cancel_workflow_async", _dbos_cancel),
    ):
        yield


TEST_DATABASE_URL = os.environ.get(
    "DRUKS_DATABASE_URL", "postgresql+psycopg://druks:druks@localhost:5432/druks"
)


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
        "redis_url": "redis://127.0.0.1:6379/0",
        "log_level": "WARNING",
    }
    defaults.update(overrides)
    return Settings(**defaults)  # type: ignore[arg-type]


class FakeLinear:
    def __init__(self, *, description: str = "") -> None:
        self._description = description

    async def get_issue(self, issue_id: str) -> dict[str, object]:
        return {"description": self._description}


@pytest.fixture(scope="session")
def _test_engine():
    # One real-transaction engine for the whole session; per-test isolation is
    # the transaction rollback in _txn, not a per-test engine or schema rebuild.
    engine = create_engine_from_url(TEST_DATABASE_URL)
    yield engine
    engine.dispose()


@pytest.fixture(scope="session", autouse=True)
def _test_schema(_test_engine):
    # Build the schema + seed reference rows (harnesses) once for the session and
    # commit them: they live outside every test's rolled-back transaction, so all
    # tests see them and none can mutate them for another. Run.state derives off
    # dbos.workflow_status, so the DBOS system tables must exist beside the app
    # schema — built here the way production builds them (DBOS.launch runs the
    # same migrations), not from DBOS's internal metadata (whose schema is an
    # unsubstituted placeholder outside the migration path).
    from dbos import run_dbos_database_migrations
    from druks.durable.dbos_state import DBOS_SYSTEM_SCHEMA
    from druks.durable.engine import _dbos_database_url

    with _test_engine.connect() as conn:
        conn.exec_driver_sql("DROP SCHEMA IF EXISTS public CASCADE")
        conn.exec_driver_sql("CREATE SCHEMA public")
        conn.exec_driver_sql(f"DROP SCHEMA IF EXISTS {DBOS_SYSTEM_SCHEMA} CASCADE")
        conn.commit()
    init_db(_test_engine)
    run_dbos_database_migrations(
        _dbos_database_url(_test_engine.url.render_as_string(hide_password=False)),
        schema=DBOS_SYSTEM_SCHEMA,
    )
    yield


@pytest.fixture
def registry_state():
    # Catalog loads and test registrations mutate the process-global MCP
    # registry; snapshot and restore so a test's entries don't leak into the
    # rest of the suite.
    from druks.extensions.registry import mcp_servers

    saved = dict(mcp_servers._items)
    yield
    mcp_servers._items.clear()
    mcp_servers._items.update(saved)


# The connection the current test's transaction is open on — what db_engine, the
# db_session fixture, and configure_app_for_test all bind to, so a seed written
# through one is visible in the others (one connection, one txn).
_test_connection: object | None = None

# These modules manage their own engine + database and commit for real — the DBOS
# durable tests (their own per-test database + worker connections that read across
# the commit) and the alembic migration test (its own AUTOCOMMIT engine, DDL it
# drops itself). The rollback model would hide or never see their writes, so they
# opt out and reset themselves. Everything else — including the durable *unit*
# tests that use the fixtures here — gets transaction rollback.
_OWN_DATABASE_MODULES = {
    "test_build_durable",
    "test_durable_sdk",
    "test_notifications_durable",
    "test_scope_durable",
    "test_usage_durable",
    "test_harness_login_persistence",
    "test_extension_migrations",
    "test_proof_extension_migration",
}


@pytest.fixture(autouse=True)
def _txn(request, _test_engine):
    # Per-test isolation by transaction rollback: open one connection, begin a
    # transaction, point the session registry and the durable step engine at it,
    # roll back at the end. Seeds are visible everywhere without committing and
    # nothing reaches the next test. (PG sequences are non-transactional, so unlike
    # the old TRUNCATE RESTART IDENTITY this does NOT reset auto-increment ids —
    # capture row.id, don't assert a literal.)
    if request.module.__name__.rsplit(".", 1)[-1] in _OWN_DATABASE_MODULES:
        yield
        return

    global _test_connection
    from druks.database import db_session as registry
    from druks.durable.engine import configure_engine

    conn = _test_engine.connect()
    txn = conn.begin()
    # The connection is owned by this fixture, not the app: a test that enters
    # the app lifespan (``with TestClient(app)``) would otherwise dispose() it on
    # teardown, but a Connection has no dispose — and we must keep it open to roll
    # back. Neutralise that one call.
    conn.dispose = lambda: None
    configure_session(conn)
    configure_engine(conn)
    _test_connection = conn
    registry.remove()
    try:
        yield
    finally:
        registry.remove()
        _test_connection = None
        configure_engine(None)
        txn.rollback()
        conn.close()


@pytest.fixture
def db_engine():
    # The per-test connection (its transaction already open); tests and the app
    # process both bind to it, so writes are visible across them without a commit.
    return _test_connection


def configure_app_for_test(
    *,
    settings: Settings,
    engine=None,
    authenticated: bool = True,
):

    from druks.accounts.dependencies import current_account
    from druks.api.app import app

    if engine is None:
        engine = _test_connection

    configure_session(engine)
    app.state.settings = settings
    app.state.engine = engine
    if authenticated:
        # Stand a signed-in account in for the gate; auth tests pass
        # authenticated=False and walk the real cookie flow.
        app.dependency_overrides[current_account] = _test_account
    return app


async def _test_account():
    from druks.accounts.models import Account

    return Account.get_or_create("op@example.com")


@pytest.fixture(autouse=True)
def _reset_app_overrides():

    yield
    from druks.api.app import app

    app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def _no_druks_namespace_fetches(monkeypatch):
    """Default every ``.druks`` namespace fetch to 404. ``render_prompt``
    with a ``repo`` (and extension-config resolution) would otherwise hit
    GitHub, which needs Extension creds tests don't have — prompts fall back to
    bundled templates, configs to their model defaults. Tests that
    exercise the override/config path patch ``fetch_file`` themselves."""

    async def _none(**_kwargs):
        return None

    monkeypatch.setattr("druks.prompts.resolver.fetch_file", _none)
    monkeypatch.setattr("druks.extensions.config.fetch_file", _none)


@pytest.fixture
def db_session(db_engine):
    # A session on the per-test connection, bound to the ambient registry so model
    # classmethods that call ``db_session()`` see the same rows the test seeds
    # directly. create_savepoint so a commit under test is a savepoint, not an end
    # to the test's outer transaction. The registry bind and the durable step engine
    # were already pointed at this connection by ``_txn``.
    from druks.database import db_session as _db_session_registry

    session = Session(db_engine, join_transaction_mode="create_savepoint", autoflush=True)
    _db_session_registry.registry.set(session)
    try:
        yield session
    finally:
        _db_session_registry.remove()
        session.close()


def bind_ambient_session(session) -> None:
    from druks.database import db_session

    db_session.registry.set(session)


def connect_harness(harness_cls, payload: dict, *, provider_email: str = "op@example.com"):
    """Seed the HarnessConnection row a finished connect flow would leave."""
    from druks.accounts.models import Account
    from druks.harnesses.models import HarnessConnection
    from druks.user_settings.models import UserSettings

    account = Account.get_or_create(provider_email)
    UserSettings.ensure_fallback_account(account.id)
    _, expires_at = harness_cls._refresh_state(payload)
    return HarnessConnection.connect(
        harness=harness_cls.name,
        account=account,
        payload=payload,
        expires_at=expires_at,
        provider_email=provider_email,
    )


def seed_dbos_status(
    session, workflow_id: str, state: str, *, subject=None, account_id=None
) -> None:
    """Write the ``dbos.workflow_status`` row a Run's derived ``state`` reads,
    carrying the subject attributes ``start()`` stamps — the paired half of
    every persisted run seed (there is no state column)."""
    from druks.durable.dbos_state import workflow_status
    from druks.models import Base

    status = {
        "scheduled": "ENQUEUED",
        "running": "PENDING",
        "pending_input": "PENDING",
        "finished": "SUCCESS",
        "failed": "ERROR",
        "cancelled": "CANCELLED",
    }[state]
    now_ms = int(Base.utc_now().timestamp() * 1000)
    attributes = {}
    if subject:
        attributes = {"subject_type": subject["type"], "subject_id": str(subject["id"])}
    if account_id:
        attributes["account_id"] = account_id
    attributes = attributes or None
    # created_at / priority carry server defaults in the dbos schema; the
    # derivation and subject keying read only these.
    session.execute(
        workflow_status.insert().values(
            workflow_uuid=workflow_id,
            status=status,
            updated_at=now_ms,
            attributes=attributes,
        )
    )
    session.flush()


def seed_build_run(
    session,
    *,
    work_item_id: int,
    state: str = "running",
    input_gate: str | None = None,
    input_request: dict | None = None,
    failure: str | None = None,
):
    """Seed a build Run row for a work item and bind it via
    ``item.build_run_id``. Returns the Run. Attach agent calls with
    ``seed_call(session, run, agent)``; the run + its calls are the item's
    timeline."""
    from druks.build.models import WorkItem
    from druks.durable import Run
    from uuid_utils import uuid7

    if state == "pending_input" and input_gate is None:
        input_gate = "review"  # a parked run always has a gate; derivation needs it
    run = Run(
        id=str(uuid7()),
        kind="build.build_workflow",
        input_gate=input_gate,
        input_request=input_request,
        failure=failure,
    )
    session.add(run)
    session.flush()
    seed_dbos_status(session, run.id, state, subject={"type": "work_item", "id": work_item_id})
    item = WorkItem.get(work_item_id)
    item.build_run_id = run.id
    session.flush()
    return run


def seed_call(session, run, agent, *, status="succeeded", last_error=None, model="gpt-5.5"):
    """Seed an AgentCall (the timeline's call row) on a run, stamped with the
    agent that made it."""
    from druks.durable import AgentCall
    from druks.models import Base

    call = AgentCall(
        run_id=run.id,
        agent=getattr(agent, "value", agent),
        model=model,
        status=status,
        last_error=last_error,
        finished_at=Base.utc_now() if status != "running" else None,
        sandbox_host_id=f"test-host-{run.id}",
    )
    session.add(call)
    session.flush()
    return call


def seed_agent_run(
    *,
    agent: str = "implement",
    repo: str = "ClawHaven/acme-app",
    work_item_id: int | None = None,
    host_id: str | None = None,
    model: str | None = "gpt-5.5",
    workflow_id: str | None = None,
):
    """Create a build Run and an AgentCall on it, returning the AgentCall.

    When ``work_item_id`` is given the run binds to it (so the call surfaces
    on that item's detail timeline); otherwise a fresh work item is created.
    Pass ``workflow_id`` to attach to an existing run instead."""
    from druks.build.models import Project, ProjectRepo, WorkItem
    from druks.database import db_session
    from druks.durable import AgentCall, Run

    session = db_session()
    if workflow_id is None:
        if work_item_id is None:
            project = Project.get_by_repo(repo)
            if project is None:
                project = Project.create(name=repo)
                ProjectRepo.create(project_id=project.id, full_name=repo)
            work_item_id = WorkItem.create(project_id=project.id, title="x", repo=repo).id
        run = seed_build_run(session, work_item_id=work_item_id)
        workflow_id = run.id
    else:
        run = Run.get(workflow_id)

    call = AgentCall(
        sandbox_host_id=host_id or f"test-host-{workflow_id}",
        model=model,
        run_id=workflow_id,
        agent=agent,
    )
    session.add(call)
    session.flush()
    return call


def seed_run(session, run_id, *, kind="build.build_workflow"):
    # A bare durable_runs row so an AgentCall / Artifact FK to it resolves.
    from druks.durable import Run

    run = Run(id=run_id, kind=kind)
    session.add(run)
    session.flush()
    return run


def make_agent_result(output, *, agent="agent", status=None, cost_usd=None, cost_metadata=None):
    # An AgentResult to return from a faked run_agent, so the agent call records/parses it.
    from datetime import UTC, datetime

    from druks.durable.enums import AgentCallStatus
    from druks.sandbox.datastructures import AgentResult

    return AgentResult(
        output=output,
        run_id="run-test",
        sandbox_host_id="host-test",
        model="claude-opus-4-7",
        agent=agent,
        status=status or AgentCallStatus.SUCCEEDED,
        started_at=datetime.now(UTC),
        cost_usd=cost_usd,
        cost_metadata=cost_metadata,
    )


def finish_agent_run(call, *, status=None, last_error=None):
    # Mark a seeded AgentCall finished (prod builds finished rows via AgentCall.record).
    from druks.database import db_session
    from druks.durable.enums import AgentCallStatus
    from druks.models import Base

    call.status = (status or AgentCallStatus.SUCCEEDED).value
    call.last_error = last_error
    call.finished_at = Base.utc_now()
    db_session().flush()
    return call


def make_test_work_item(*, repo: str, **kwargs):
    """Create a WorkItem with the required Project / ProjectRepo binding
    for tests. Looks up an existing Project by repo name; otherwise
    creates the chain. Extra kwargs flow into ``WorkItem.create``."""
    from druks.build.models import Project, ProjectRepo, WorkItem

    project = Project.get_by_repo(repo)
    if project is None:
        project = Project.create(name=repo)
        ProjectRepo.create(project_id=project.id, full_name=repo)
    return WorkItem.create(project_id=project.id, repo=repo, **kwargs)

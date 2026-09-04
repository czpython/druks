from unittest import mock

import pytest
from druks.apps.loader import (
    iter_apps,
    register_workflow_package,
)
from druks.database import create_engine_from_url
from druks.durable.dbos_state import DBOS_SYSTEM_SCHEMA
from druks.models import Base
from druks.testing import TEST_DATABASE_URL

# A Workflow class resolves its declaring app at definition time, from
# packages the loader registers before importing. Tests import workflow modules
# directly and some declare their own workflows, so both register here — before
# collection imports any test module.
iter_apps()
for test_module in ("test_durable_sdk", "test_notifications_durable"):
    register_workflow_package(test_module, "")


@pytest.fixture(autouse=True)
async def _redis(druks_redis):
    yield


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

        await db_session().execute(
            update(workflow_status)
            .where(workflow_status.c.workflow_uuid == workflow_id)
            .values(status="CANCELLED")
        )

    with (
        mock.patch.object(Workflow, "start", classmethod(_noop)),
        mock.patch("druks.agents.set_run_phase", _phase_noop),
        mock.patch("dbos.DBOS.cancel_workflow_async", _dbos_cancel),
    ):
        yield


@pytest.fixture(scope="session", autouse=True)
def _reset_test_database():
    # The repository suite owns its scratch database and starts from a clean
    # application and DBOS schema; the shipped fixtures never destroy schemas. This
    # runs before any test, so the shipped session fixture builds onto the fresh one.
    engine = create_engine_from_url(TEST_DATABASE_URL)
    with engine.connect() as connection:
        connection.exec_driver_sql("DROP SCHEMA IF EXISTS public CASCADE")
        connection.exec_driver_sql("CREATE SCHEMA public")
        connection.exec_driver_sql(f"DROP SCHEMA IF EXISTS {DBOS_SYSTEM_SCHEMA} CASCADE")
        connection.commit()
    engine.dispose()
    yield


@pytest.fixture
def registry_state():
    # Catalog loads and test registrations mutate the process-global MCP
    # registry; snapshot and restore so a test's entries don't leak into the
    # rest of the suite.
    from druks.apps.registry import mcp_servers

    saved = dict(mcp_servers._items)
    yield
    mcp_servers._items.clear()
    mcp_servers._items.update(saved)


@pytest.fixture
def browser_session_declarations():
    # BrowserSession declarations self-register at class definition, so what a
    # test module defines at import time would leak into every merged-list
    # read; tests declare inside this fixture and leave the registry as found.
    from druks.apps.registry import browser_sessions

    saved = dict(browser_sessions._items)
    yield browser_sessions
    browser_sessions._items.clear()
    browser_sessions._items.update(saved)


# These modules manage their own engine + database and commit for real — the DBOS
# durable tests (their own per-test database + worker connections that read across
# the commit) and the alembic migration test (its own AUTOCOMMIT engine, DDL it
# drops itself). The rollback model would hide or never see their writes, so they
# opt out and reset themselves. Everything else — including the durable *unit*
# tests that use the fixtures here — gets transaction rollback.
_OWN_DATABASE_MODULES = {
    "test_durable_sdk",
    "test_notifications_durable",
    "test_provider_login_persistence",
    "test_app_migrations",
    "test_proof_app_migration",
}


def pytest_collection_modifyitems(items):
    # druks_db is async, so a sync fixture can't getfixturevalue it any more;
    # injecting it into each test's fixture list keeps the same guarantee —
    # every test outside the own-database modules runs inside the rollback
    # transaction.
    for item in items:
        module = item.module.__name__.rsplit(".", 1)[-1]
        if module in _OWN_DATABASE_MODULES:
            continue
        if not hasattr(item, "fixturenames") or "druks_db" in item.fixturenames:
            continue
        item.fixturenames.append("druks_db")


@pytest.fixture(autouse=True)
def _reset_app_overrides():

    yield
    from druks.api.server import app

    app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def _no_druks_namespace_fetches(monkeypatch):
    """Default every ``.druks`` namespace fetch to 404. ``render_prompt``
    with a ``repo`` (and app-config resolution) would otherwise hit
    GitHub, which needs App creds tests don't have — prompts fall back to
    bundled templates, configs to their model defaults. Tests that
    exercise the override/config path patch ``fetch_file`` themselves."""

    async def _none(**_kwargs):
        return None

    monkeypatch.setattr("druks.prompts.resolver.fetch_file", _none)
    monkeypatch.setattr("druks.apps.config.fetch_file", _none)


def bind_ambient_session(session) -> None:
    from druks.database import db_session

    db_session.registry.set(session)


async def connect_provider(provider_cls, payload: dict, *, provider_email: str = "op@example.com"):
    """Seed the ProviderLogin row a finished OAuth connect flow would leave."""
    from druks.accounts.models import Account
    from druks.harnesses.models import ProviderLogin
    from druks.user_settings.models import UserSettings

    account = await Account.get_or_create(provider_email)
    settings = await UserSettings.get()
    if not settings.fallback_account_id:
        await settings.set_fallback_account(account.id)
    _, expires_at = provider_cls._refresh_state(payload)
    return await ProviderLogin.connect(
        provider=provider_cls.id,
        account=account,
        payload=payload,
        expires_at=expires_at,
        provider_email=provider_email,
        kind="oauth",
    )


def make_agent_result(output, *, agent="agent", error=None, cost_usd=None, cost_metadata=None):
    # An AgentResult to return from a faked run_agent, so the agent call records/parses it.
    # Status follows the error, as the sandbox does it — a failed result always says why.
    from datetime import UTC, datetime

    from druks.durable.enums import AgentCallStatus
    from druks.sandbox.datastructures import AgentResult

    return AgentResult(
        output=output,
        run_id="run-test",
        sandbox_host_id="host-test",
        model="anthropic/claude-opus-4-7",
        agent=agent,
        status=AgentCallStatus.FAILED if error else AgentCallStatus.SUCCEEDED,
        started_at=datetime.now(UTC),
        cost_usd=cost_usd,
        cost_metadata=cost_metadata,
        error=error,
    )


async def finish_agent_run(call, *, status=None, last_error=None):
    # Mark a seeded AgentCall finished (prod builds finished rows via AgentCall.record).
    from druks.database import db_session
    from druks.durable.enums import AgentCallStatus

    call.status = (status or AgentCallStatus.SUCCEEDED).value
    call.last_error = last_error
    call.finished_at = Base.utc_now()
    await db_session().flush()
    return call


async def make_test_note(body: str = "a note"):
    """The platform suite's subject. It belongs to the proof app, not to software_factory —
    platform behavior must hold for any app's rows."""
    from druks_field_notes.models import Note

    return await Note.create(body=body)


async def seed_note_run(session, *, note=None, state: str = "running", **kwargs):
    """A run on a note, seeding one if the caller has none."""
    from druks.testing import seed_run
    from druks_field_notes.workflows import Summarize

    subject = note or await make_test_note()
    return await seed_run(session, kind=Summarize.kind, subject=subject, state=state, **kwargs)


async def seed_note_agent_run(*, agent: str = "implement", model: str = "openai/gpt-5.5", **kwargs):
    """A run on a fresh note with one agent call on it — the call is what the caller wants."""
    from druks.database import db_session
    from druks.testing import seed_call

    session = db_session()
    run = await seed_note_run(session, **kwargs)
    return await seed_call(session, run, agent, status="running", model=model)

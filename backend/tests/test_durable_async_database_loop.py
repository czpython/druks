import asyncio
import os

import psycopg
import pytest
import pytest_asyncio
from dbos import DBOS, DBOSConfig, StepOptions
from dbos._dbos import _get_or_create_dbos_registry
from druks.durable.dbos_state import DBOS_SYSTEM_SCHEMA
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

POSTGRES_BASE_URL = os.environ.get("DRUKS_TEST_PG", "postgresql://druks:druks@localhost:5432")
DATABASE_NAME = "druks_durable_async_database_loop_test"
DATABASE_URL = (
    f"{POSTGRES_BASE_URL.replace('postgresql://', 'postgresql+psycopg://')}/{DATABASE_NAME}"
)
DBOS_DATABASE_URL = DATABASE_URL.replace("postgresql+psycopg://", "postgresql://", 1)


def _pg_up() -> bool:
    try:
        psycopg.connect(f"{POSTGRES_BASE_URL}/postgres", connect_timeout=2).close()
        return True
    except psycopg.Error:
        return False


pytestmark = [
    pytest.mark.skipif(not _pg_up(), reason="test Postgres not reachable"),
    pytest.mark.asyncio(loop_scope="module"),
]

ASYNC_ENGINE: AsyncEngine | None = None
ASYNC_ENGINE_LOOP: asyncio.AbstractEventLoop | None = None


def _build_workflow():
    @DBOS.workflow(name="test.async_database_loop")
    async def write_with_async_session_in_dbos_step() -> str | None:
        engine = ASYNC_ENGINE
        engine_loop = ASYNC_ENGINE_LOOP
        assert engine
        assert engine_loop

        async def write_row() -> str | None:
            assert asyncio.get_running_loop() is engine_loop
            async with AsyncSession(engine) as session:
                await session.execute(
                    text("CREATE TABLE dbos_async_loop_rows (value TEXT PRIMARY KEY)")
                )
                await session.execute(
                    text("INSERT INTO dbos_async_loop_rows (value) VALUES ('same-loop')")
                )
                value = await session.scalar(text("SELECT value FROM dbos_async_loop_rows"))
                await session.commit()
                return value

        return await DBOS.run_step_async(StepOptions(name="test.async_database_write"), write_row)

    return write_with_async_session_in_dbos_step


@pytest_asyncio.fixture(scope="module", autouse=True, loop_scope="module")
async def workflow():
    global ASYNC_ENGINE, ASYNC_ENGINE_LOOP

    with psycopg.connect(f"{POSTGRES_BASE_URL}/postgres", autocommit=True) as admin:
        admin.execute(f"DROP DATABASE IF EXISTS {DATABASE_NAME} WITH (FORCE)")
        admin.execute(f"CREATE DATABASE {DATABASE_NAME}")

    built = _build_workflow()
    config: DBOSConfig = {
        "name": "druks-async-database-loop-test",
        "application_database_url": DBOS_DATABASE_URL,
        "system_database_url": DBOS_DATABASE_URL,
        "dbos_system_schema": DBOS_SYSTEM_SCHEMA,
        "run_admin_server": False,
    }
    DBOS(config=config)
    async_engine = create_async_engine(DATABASE_URL)
    ASYNC_ENGINE = async_engine
    ASYNC_ENGINE_LOOP = asyncio.get_running_loop()
    DBOS.launch()
    try:
        yield built
    finally:
        DBOS.destroy()
        # destroy() leaves the registry pointing at a dead launched instance, so
        # scheduled registrations would submit pollers to its torn-down executor;
        # None restores the queue-for-launch path.
        _get_or_create_dbos_registry().dbos = None
        await async_engine.dispose()
        ASYNC_ENGINE = None
        ASYNC_ENGINE_LOOP = None


async def test_dbos_step_owns_async_database_engine_loop(workflow):
    value = await workflow()
    assert value == "same-loop"

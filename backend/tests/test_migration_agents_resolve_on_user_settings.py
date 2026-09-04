import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text

# Postgres DDL is transactional, so the test rebuilds the pre-migration shape
# inside the suite's rolled-back transaction and runs the real upgrade on it.
_MIGRATION = (
    Path(__file__).resolve().parent.parent
    / "migrations"
    / "versions"
    / "7e1c4b9d2a58_agents_resolve_on_user_settings.py"
)


def _upgrade(connection) -> None:
    spec = importlib.util.spec_from_file_location("agents_resolve", _MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with Operations.context(MigrationContext.configure(connection)):
        module.upgrade()


async def test_the_defaults_come_from_the_first_harness_row(druks_db):
    for statement in (
        "ALTER TABLE user_settings DROP COLUMN default_harness, DROP COLUMN default_billing, "
        "DROP COLUMN default_effort, DROP COLUMN fast_mode, DROP COLUMN default_timeout",
        "CREATE TABLE harnesses (name varchar PRIMARY KEY, fast_mode boolean NOT NULL, "
        "effort varchar NOT NULL, timeout integer NOT NULL, updated_at timestamptz NOT NULL)",
        "INSERT INTO harnesses VALUES ('codex', false, 'high', 1800, now()), "
        "('claude', true, 'low', 600, now())",
        "INSERT INTO user_settings (id, timezone, default_model, updated_at) "
        "VALUES (1, 'UTC', 'anthropic/claude-opus-4-7', now()) ON CONFLICT (id) DO NOTHING",
    ):
        await druks_db.execute(text(statement))

    await (await druks_db.connection()).run_sync(_upgrade)

    row = (
        await druks_db.execute(
            text(
                "SELECT default_harness, default_billing, default_effort, fast_mode, "
                "default_timeout FROM user_settings"
            )
        )
    ).one()
    assert tuple(row) == ("claude", "subscription", "low", True, 600)
    assert (await druks_db.execute(text("SELECT to_regclass('harnesses')"))).scalar() is None

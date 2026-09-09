import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from druks.testing import seed_run
from druks.user_settings.models import SettingsOverride
from sqlalchemy import text

# Data-only, so it runs inside the suite's rolled-back transaction against the
# current schema.
_MIGRATION = (
    Path(__file__).resolve().parent.parent
    / "migrations"
    / "versions"
    / "d2f7a9c4e816_app_qualified_agent_ids.py"
)
_STEPS = "SELECT function_name FROM dbos.operation_outputs ORDER BY workflow_uuid, function_id"
_OVERRIDES = "SELECT key FROM settings_overrides ORDER BY key"


async def _apply(druks_db, step) -> None:
    def run(connection) -> None:
        with Operations.context(MigrationContext.configure(connection)):
            step()

    await druks_db.flush()
    await (await druks_db.connection()).run_sync(run)


async def _column(druks_db, statement: str) -> list[str]:
    return list((await druks_db.execute(text(statement))).scalars())


async def test_bundled_agent_overrides_and_steps_take_the_apps_name(druks_db):
    await SettingsOverride.set_agent_model("implement", "openai/gpt-5.5")
    await SettingsOverride.set_agent_effort("review_pull_request", "low")
    # An agent of an app the migration does not know keeps its flat name.
    await SettingsOverride.set_agent_timeout("engage", 90)
    await SettingsOverride.set_workflow_setting("software_factory.build", "review_code", False)
    await seed_run(druks_db, kind="software_factory.build", run_id="build-run", state="failed")
    await seed_run(druks_db, kind="x_me.engage", run_id="engage-run", state="failed")
    for run_id, function_id, step in (
        ("build-run", 1, "software_factory.build.agent.implement"),
        ("build-run", 2, "software_factory.build.agent.implement.retry_wait"),
        ("engage-run", 1, "x_me.engage.agent.engage"),
    ):
        await druks_db.execute(
            text(
                "INSERT INTO dbos.operation_outputs "
                "(workflow_uuid, function_id, function_name, error) "
                "VALUES (:run, :function_id, :step, 'boom')"
            ),
            {"run": run_id, "function_id": function_id, "step": step},
        )
    spec = importlib.util.spec_from_file_location("app_qualified_agent_ids", _MIGRATION)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    await _apply(druks_db, migration.upgrade)

    assert await _column(druks_db, _OVERRIDES) == [
        "agent_effort:software_factory.review_pull_request",
        "agent_model:software_factory.implement",
        "agent_timeout:engage",
        "workflow:software_factory.build:review_code",
    ]
    assert await _column(druks_db, _STEPS) == [
        "software_factory.build.agent.software_factory.implement",
        "software_factory.build.agent.software_factory.implement.retry_wait",
        "x_me.engage.agent.engage",
    ]

    await _apply(druks_db, migration.downgrade)

    assert await _column(druks_db, _OVERRIDES) == [
        "agent_effort:review_pull_request",
        "agent_model:implement",
        "agent_timeout:engage",
        "workflow:software_factory.build:review_code",
    ]
    assert await _column(druks_db, _STEPS) == [
        "software_factory.build.agent.implement",
        "software_factory.build.agent.implement.retry_wait",
        "x_me.engage.agent.engage",
    ]

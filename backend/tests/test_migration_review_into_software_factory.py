import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from druks.testing import seed_run
from sqlalchemy import text

_MIGRATION = (
    Path(__file__).resolve().parent.parent
    / "migrations"
    / "versions"
    / "b4d7e2a9c1f6_merge_review_into_software_factory.py"
)
_OLD = "review.pull_request_review"
_NEW = "software_factory.pull_request_review"


def _migration():
    spec = importlib.util.spec_from_file_location("merge_review", _MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _upgrade(connection) -> None:
    with Operations.context(MigrationContext.configure(connection)):
        _migration().upgrade()


def _downgrade(connection) -> None:
    with Operations.context(MigrationContext.configure(connection)):
        _migration().downgrade()


async def _rows(druks_db, sql: str) -> list[tuple]:
    return [tuple(row) for row in await druks_db.execute(text(sql))]


async def test_the_review_run_keeps_its_dbos_names_and_build_events_keep_their_owner(druks_db):
    await seed_run(druks_db, kind=_OLD, run_id="review-run", state="failed")
    await druks_db.execute(
        text("UPDATE dbos.workflow_status SET name = :old WHERE workflow_uuid = 'review-run'"),
        {"old": _OLD},
    )
    await druks_db.execute(
        text(
            "INSERT INTO dbos.operation_outputs (workflow_uuid, function_id, function_name, error) "
            "VALUES ('review-run', 1, :step, 'boom')"
        ),
        {"step": f"{_OLD}.agent.review_pull_request"},
    )
    await druks_db.execute(
        text(
            "INSERT INTO events (type, app, created_at, payload) VALUES "
            "('finished', 'software_factory', now(), '{\"kind\": \"software_factory.build\"}')"
        )
    )
    connection = await druks_db.connection()

    await connection.run_sync(_upgrade)

    # A retry forks the run under the name DBOS registered it with and replays
    # each step under its recorded name; both follow the kind.
    assert await _rows(
        druks_db, "SELECT name FROM dbos.workflow_status WHERE workflow_uuid = 'review-run'"
    ) == [(_NEW,)]
    assert await _rows(
        druks_db,
        "SELECT function_name FROM dbos.operation_outputs WHERE workflow_uuid = 'review-run'",
    ) == [(f"{_NEW}.agent.review_pull_request",)]

    await connection.run_sync(_downgrade)

    assert await _rows(druks_db, "SELECT app FROM events") == [("software_factory",)]

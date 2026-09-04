from pathlib import Path

from alembic import command
from alembic.config import Config
from druks.testing import TEST_DATABASE_URL, init_db
from sqlalchemy import create_engine

_ALEMBIC_INI = Path(__file__).resolve().parent.parent / "alembic.ini"
_VERSIONS = (
    Path(__file__).resolve().parent.parent
    / "druks"
    / "contrib"
    / "issues"
    / "migrations"
    / "versions"
)
_TABLES = "issues_comments, issues_tickets, issues_projects, alembic_version_issues"


def _config() -> Config:
    config = Config(str(_ALEMBIC_INI))
    config.set_main_option("version_locations", str(_VERSIONS))
    config.set_main_option("sqlalchemy.url", TEST_DATABASE_URL)
    config.attributes["version_table"] = "alembic_version_issues"
    return config


def _drop(conn) -> None:
    conn.exec_driver_sql(f"DROP TABLE IF EXISTS {_TABLES}")


def test_issues_migration_applies_under_its_own_version_table(request):
    engine = create_engine(TEST_DATABASE_URL, isolation_level="AUTOCOMMIT")
    with engine.connect() as conn:
        _drop(conn)
    try:
        command.upgrade(_config(), "head")
        with engine.connect() as conn:
            assert conn.exec_driver_sql("SELECT to_regclass('issues_projects')").scalar()
            assert conn.exec_driver_sql("SELECT to_regclass('issues_tickets')").scalar()
            assert conn.exec_driver_sql("SELECT to_regclass('issues_comments')").scalar()
            head = conn.exec_driver_sql("SELECT version_num FROM alembic_version_issues").scalar()
            assert head == "issues_0001"
    finally:
        with engine.connect() as conn:
            _drop(conn)
        init_db(engine)
        engine.dispose()
        request.getfixturevalue("_druks_engine").dispose()

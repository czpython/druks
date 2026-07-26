import os
from pathlib import Path

from alembic import command
from alembic.config import Config
from druks.testing import init_db
from sqlalchemy import create_engine

# The proof extension's real, shipped migration, run through the shared platform env
# exactly as ``druks init-db`` runs an installed extension's — from the package's own
# ``version_locations`` under its own ``alembic_version_field_notes`` history. Proves an
# out-of-tree package's hand-written baseline applies cleanly and tracks its own head,
# independent of core's. Manages its own DDL (via an AUTOCOMMIT engine) and cleans up, so
# it opts out of the suite's transaction-rollback isolation (conftest ``_OWN_DATABASE_MODULES``).
_ALEMBIC_INI = Path(__file__).resolve().parent.parent / "alembic.ini"
_PACKAGE_ROOT = Path(__file__).resolve().parent / "druks-field_notes" / "druks_field_notes"
_VERSIONS = _PACKAGE_ROOT / "migrations" / "versions"

TEST_DATABASE_URL = os.environ.get(
    "DRUKS_DATABASE_URL", "postgresql+psycopg://druks:druks@localhost:5432/druks"
)


def _config() -> Config:
    config = Config(str(_ALEMBIC_INI))
    config.set_main_option("version_locations", str(_VERSIONS))
    config.set_main_option("sqlalchemy.url", TEST_DATABASE_URL)
    config.attributes["version_table"] = "alembic_version_field_notes"
    return config


def _drop(conn) -> None:
    conn.exec_driver_sql("DROP TABLE IF EXISTS field_notes_notes, alembic_version_field_notes")


def test_proof_migration_applies_under_its_own_version_table(request):
    """The proof package's baseline creates its table and records its head in
    ``alembic_version_field_notes`` — its own history, not core's shared table."""
    engine = create_engine(TEST_DATABASE_URL, isolation_level="AUTOCOMMIT")
    with engine.connect() as conn:
        _drop(conn)
    try:
        command.upgrade(_config(), "head")
        with engine.connect() as conn:
            table = conn.exec_driver_sql("SELECT to_regclass('field_notes_notes')").scalar()
            assert table is not None
            head = conn.exec_driver_sql(
                "SELECT version_num FROM alembic_version_field_notes"
            ).scalar()
            assert head == "field_notes_0001"
    finally:
        with engine.connect() as conn:
            _drop(conn)
        # field_notes is installed, so its table is part of the schema the rest of the
        # suite reads; put it back for the tests that follow.
        init_db(engine)
        engine.dispose()
        request.getfixturevalue("_druks_engine").dispose()

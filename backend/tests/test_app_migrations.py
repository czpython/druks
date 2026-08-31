from pathlib import Path
from unittest.mock import MagicMock

from alembic import command
from alembic.config import Config
from druks.database import make_app_migration
from druks.files import File, FileField
from druks.models import Base
from druks.testing import TEST_DATABASE_URL
from sqlalchemy import Column, Integer, MetaData, String, Table, create_engine
from sqlalchemy.orm import Mapped, mapped_column

# The real platform ``alembic.ini`` — its script_location is the one shared env.py
# that serves every app. These tests run a synthetic app's revisions through it
# from an external version_locations, proving the target shape: shared env, the
# app's own version_locations and version_table, isolated from core's history.
_ALEMBIC_INI = Path(__file__).resolve().parent.parent / "alembic.ini"


class MigrationProbeFile(Base):
    __tablename__ = "migration_probe_files"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    image: Mapped[File] = FileField()


_BASELINE = """\
import sqlalchemy as sa
from alembic import op

revision = "ext0001"
down_revision = None


def upgrade() -> None:
    op.create_table("ext_probe", sa.Column("id", sa.Integer, primary_key=True))


def downgrade() -> None:
    op.drop_table("ext_probe")
"""


def _versions_dir(tmp_path) -> Path:
    versions = tmp_path / "versions"
    versions.mkdir()
    (versions / "0001_baseline.py").write_text(_BASELINE)
    return versions


def _config(versions, *, version_table, target_metadata=None) -> Config:
    config = Config(str(_ALEMBIC_INI))
    config.set_main_option("version_locations", str(versions))
    config.set_main_option("sqlalchemy.url", TEST_DATABASE_URL)
    config.attributes["version_table"] = version_table
    if target_metadata is not None:
        config.attributes["target_metadata"] = target_metadata
    return config


def _drop(conn) -> None:
    conn.exec_driver_sql("DROP TABLE IF EXISTS ext_probe, alembic_version_ext, alembic_version")


def test_app_upgrade_runs_through_platform_env_with_its_own_version_table(tmp_path):
    """An app's revisions, run through the shared platform env from an external
    ``version_locations``, track their head in the app's own
    ``alembic_version_<app>`` — so a foreign head in core's default table doesn't
    derail them, and core's own scripts (the env's default ``versions/``) don't
    leak in."""
    versions = _versions_dir(tmp_path)
    engine = create_engine(TEST_DATABASE_URL, isolation_level="AUTOCOMMIT")
    with engine.connect() as conn:
        _drop(conn)
        # Core already at head: the shared default table holds a revision the app's
        # own history has never heard of.
        conn.exec_driver_sql("CREATE TABLE alembic_version (version_num varchar(32) NOT NULL)")
        conn.exec_driver_sql("INSERT INTO alembic_version VALUES ('b7e4f0a1c2d3')")
    try:
        command.upgrade(_config(versions, version_table="alembic_version_ext"), "head")
        with engine.connect() as conn:
            assert conn.exec_driver_sql("SELECT to_regclass('ext_probe')").scalar()
            assert (
                conn.exec_driver_sql("SELECT version_num FROM alembic_version_ext").scalar()
                == "ext0001"
            )
            # Core's default table is untouched — version_locations replaced the
            # default, so core's own scripts never ran in the app's pass.
            assert (
                conn.exec_driver_sql("SELECT version_num FROM alembic_version").scalar()
                == "b7e4f0a1c2d3"
            )
    finally:
        with engine.connect() as conn:
            _drop(conn)
        engine.dispose()


def test_app_autogenerate_scopes_to_the_app_metadata(tmp_path):
    """``revision --autogenerate`` through the platform env diffs only the scoped
    metadata against the live DB and writes the revision into the app's own
    ``versions/`` — reflected tables it doesn't own are left alone."""
    versions = _versions_dir(tmp_path)
    engine = create_engine(TEST_DATABASE_URL, isolation_level="AUTOCOMMIT")
    with engine.connect() as conn:
        _drop(conn)
    try:
        # Baseline applied: the DB has ext_probe(id) at the app's head.
        command.upgrade(_config(versions, version_table="alembic_version_ext"), "head")

        scoped = MetaData()
        Table("ext_probe", scoped, Column("id", Integer, primary_key=True), Column("note", String))
        before = set(versions.glob("*.py"))
        command.revision(
            _config(versions, version_table="alembic_version_ext", target_metadata=scoped),
            message="add note",
            autogenerate=True,
        )
        (generated,) = set(versions.glob("*.py")) - before
        body = generated.read_text()
        assert "add_column" in body
        assert "note" in body
        # Only ext_probe is diffed; the reflected version table isn't dropped.
        assert "drop_table" not in body
    finally:
        with engine.connect() as conn:
            _drop(conn)
        engine.dispose()


def test_file_field_autogenerates_and_upgrades_with_the_platform_foreign_key(
    _druks_schema, tmp_path, monkeypatch
):
    """An app migration renders FileField as a String FK without owning the files table."""
    engine = create_engine(TEST_DATABASE_URL, isolation_level="AUTOCOMMIT")
    package_dir = tmp_path / "migration_probe"
    versions = package_dir / "migrations" / "versions"
    versions.mkdir(parents=True)
    app = MagicMock()
    app.name = "migration_probe"
    app.table_prefix = "migration_probe_"
    app.package_dir.return_value = package_dir
    monkeypatch.setattr("druks.apps.loader.get_app", lambda name: app)

    with engine.connect() as connection:
        connection.exec_driver_sql(
            "DROP TABLE IF EXISTS migration_probe_files, alembic_version_migration_probe"
        )
        connection.commit()
    try:
        make_app_migration(
            "migration_probe",
            "add file reference",
            TEST_DATABASE_URL,
        )
        [revision] = versions.glob("*.py")
        body = revision.read_text()
        assert "create_table('migration_probe_files'" in body
        assert "sa.String()" in body
        assert "sa.ForeignKeyConstraint(['image'], ['files.id']" in body
        # Only the app's own table: files and everything it names ride along to
        # resolve foreign keys, and nothing more.
        assert body.count("create_table(") == 1

        command.upgrade(
            _config(
                versions,
                version_table="alembic_version_migration_probe",
            ),
            "head",
        )
        with engine.connect() as connection:
            foreign_key = connection.exec_driver_sql(
                "SELECT ccu.table_name || '.' || ccu.column_name "
                "FROM information_schema.table_constraints tc "
                "JOIN information_schema.constraint_column_usage ccu "
                "ON ccu.constraint_name = tc.constraint_name "
                "WHERE tc.table_name = 'migration_probe_files' "
                "AND tc.constraint_type = 'FOREIGN KEY'"
            ).scalar()
            assert foreign_key == "files.id"
    finally:
        with engine.connect() as connection:
            connection.exec_driver_sql(
                "DROP TABLE IF EXISTS migration_probe_files, alembic_version_migration_probe"
            )
        engine.dispose()

from pathlib import Path

from alembic import command
from alembic.config import Config
from druks.testing import TEST_DATABASE_URL
from sqlalchemy import Column, Integer, MetaData, String, Table, create_engine

# The real platform ``alembic.ini`` — its script_location is the one shared env.py
# that serves every extension. These tests run a synthetic extension's revisions through it
# from an external version_locations, proving the target shape: shared env, the
# extension's own version_locations and version_table, isolated from core's history.
_ALEMBIC_INI = Path(__file__).resolve().parent.parent / "alembic.ini"


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


def test_extension_upgrade_runs_through_platform_env_with_its_own_version_table(tmp_path):
    """An extension's revisions, run through the shared platform env from an external
    ``version_locations``, track their head in the extension's own
    ``alembic_version_<extension>`` — so a foreign head in core's default table doesn't
    derail them, and core's own scripts (the env's default ``versions/``) don't
    leak in."""
    versions = _versions_dir(tmp_path)
    engine = create_engine(TEST_DATABASE_URL, isolation_level="AUTOCOMMIT")
    with engine.connect() as conn:
        _drop(conn)
        # Core already at head: the shared default table holds a revision the extension's
        # own history has never heard of.
        conn.exec_driver_sql("CREATE TABLE alembic_version (version_num varchar(32) NOT NULL)")
        conn.exec_driver_sql("INSERT INTO alembic_version VALUES ('b7e4f0a1c2d3')")
    try:
        command.upgrade(_config(versions, version_table="alembic_version_ext"), "head")
        with engine.connect() as conn:
            assert conn.exec_driver_sql("SELECT to_regclass('ext_probe')").scalar() is not None
            assert (
                conn.exec_driver_sql("SELECT version_num FROM alembic_version_ext").scalar()
                == "ext0001"
            )
            # Core's default table is untouched — version_locations replaced the
            # default, so core's own scripts never ran in the extension's pass.
            assert (
                conn.exec_driver_sql("SELECT version_num FROM alembic_version").scalar()
                == "b7e4f0a1c2d3"
            )
    finally:
        with engine.connect() as conn:
            _drop(conn)
        engine.dispose()


def test_extension_autogenerate_scopes_to_the_extension_metadata(tmp_path):
    """``revision --autogenerate`` through the platform env diffs only the scoped
    metadata against the live DB and writes the revision into the extension's own
    ``versions/`` — reflected tables it doesn't own are left alone."""
    versions = _versions_dir(tmp_path)
    engine = create_engine(TEST_DATABASE_URL, isolation_level="AUTOCOMMIT")
    with engine.connect() as conn:
        _drop(conn)
    try:
        # Baseline applied: the DB has ext_probe(id) at the extension's head.
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

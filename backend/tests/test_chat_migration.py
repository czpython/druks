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
    / "chat"
    / "migrations"
    / "versions"
)
_TABLES = "chat_messages, chat_conversations, alembic_version_chat"


def _config() -> Config:
    config = Config(str(_ALEMBIC_INI))
    config.set_main_option("version_locations", str(_VERSIONS))
    config.set_main_option("sqlalchemy.url", TEST_DATABASE_URL)
    config.attributes["version_table"] = "alembic_version_chat"
    return config


def _drop(conn) -> None:
    conn.exec_driver_sql(f"DROP TABLE IF EXISTS {_TABLES}")


def test_chat_migration_applies_under_its_own_version_table(request):
    engine = create_engine(TEST_DATABASE_URL, isolation_level="AUTOCOMMIT")
    with engine.connect() as conn:
        _drop(conn)
    try:
        command.upgrade(_config(), "head")
        with engine.connect() as conn:
            assert conn.exec_driver_sql("SELECT to_regclass('chat_conversations')").scalar()
            assert conn.exec_driver_sql("SELECT to_regclass('chat_messages')").scalar()
            head = conn.exec_driver_sql("SELECT version_num FROM alembic_version_chat").scalar()
            assert head == "chat_0001"
    finally:
        with engine.connect() as conn:
            _drop(conn)
        init_db(engine)
        engine.dispose()
        request.getfixturevalue("_druks_engine").dispose()

import os
from pathlib import Path

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

_ALEMBIC_INI = Path(__file__).resolve().parent.parent / "alembic.ini"
_PG_BASE = os.environ.get("DRUKS_TEST_PG", "postgresql://druks:druks@localhost:5432")
_DATABASE_NAME = "druks_work_item_ticket_migration_test"
_DATABASE_URL = (
    f"{_PG_BASE.replace('postgresql://', 'postgresql+psycopg://')}/{_DATABASE_NAME}"
)


def _postgres_is_reachable() -> bool:
    try:
        psycopg.connect(f"{_PG_BASE}/postgres", connect_timeout=2).close()
        return True
    except psycopg.Error:
        return False


def _config() -> Config:
    config = Config(str(_ALEMBIC_INI))
    config.set_main_option("sqlalchemy.url", _DATABASE_URL)
    return config


def _recreate_database() -> None:
    with psycopg.connect(f"{_PG_BASE}/postgres", autocommit=True) as connection:
        connection.execute(f'DROP DATABASE IF EXISTS "{_DATABASE_NAME}" WITH (FORCE)')
        connection.execute(f'CREATE DATABASE "{_DATABASE_NAME}"')


def _drop_database() -> None:
    with psycopg.connect(f"{_PG_BASE}/postgres", autocommit=True) as connection:
        connection.execute(f'DROP DATABASE IF EXISTS "{_DATABASE_NAME}" WITH (FORCE)')


@pytest.mark.skipif(not _postgres_is_reachable(), reason="test Postgres not reachable")
def test_work_item_ticket_fields_upgrade_and_downgrade_preserve_schema_and_data():
    _recreate_database()
    engine = create_engine(_DATABASE_URL)
    try:
        command.upgrade(_config(), "d3a5c71f8e40")
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO projects (id, name, created_at, updated_at)
                    VALUES (1, 'druks', now(), now())
                    """
                )
            )
            connection.execute(
                text(
                    """
                    INSERT INTO work_items (
                        id, project_id, source, title, remote_key, remote_url, repo,
                        created_at, updated_at
                    )
                    VALUES (
                        42, 1, 'linear', 'Rename the fields', 'ENG-755',
                        'https://linear.app/fellaworks/issue/ENG-755/example',
                        'czpython/druks', now(), now()
                    )
                    """
                )
            )

        command.upgrade(_config(), "head")
        with engine.connect() as connection:
            columns = {
                column["name"]: column for column in inspect(connection).get_columns("work_items")
            }
            indexes = {
                index["name"]: index for index in inspect(connection).get_indexes("work_items")
            }
            row = connection.execute(
                text("SELECT ticket_key, ticket_url FROM work_items WHERE id = 42")
            ).one()

            assert "remote_key" not in columns
            assert "remote_url" not in columns
            assert columns["ticket_key"]["nullable"] is False
            assert columns["ticket_url"]["nullable"] is True
            assert "work_items_remote_unique" not in indexes
            assert indexes["work_items_ticket_unique"]["column_names"] == [
                "source",
                "ticket_key",
            ]
            assert indexes["work_items_ticket_unique"]["unique"] is True
            assert tuple(row) == (
                "ENG-755",
                "https://linear.app/fellaworks/issue/ENG-755/example",
            )

        command.downgrade(_config(), "d3a5c71f8e40")
        with engine.connect() as connection:
            columns = {
                column["name"]: column for column in inspect(connection).get_columns("work_items")
            }
            indexes = {
                index["name"]: index for index in inspect(connection).get_indexes("work_items")
            }
            row = connection.execute(
                text("SELECT remote_key, remote_url FROM work_items WHERE id = 42")
            ).one()

            assert "ticket_key" not in columns
            assert "ticket_url" not in columns
            assert columns["remote_key"]["nullable"] is False
            assert columns["remote_url"]["nullable"] is True
            assert "work_items_ticket_unique" not in indexes
            assert indexes["work_items_remote_unique"]["column_names"] == [
                "source",
                "remote_key",
            ]
            assert indexes["work_items_remote_unique"]["unique"] is True
            assert tuple(row) == (
                "ENG-755",
                "https://linear.app/fellaworks/issue/ENG-755/example",
            )
    finally:
        engine.dispose()
        _drop_database()

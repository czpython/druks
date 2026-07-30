import json
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from druks.testing import TEST_DATABASE_URL, init_db
from sqlalchemy import create_engine, text

_ALEMBIC_INI = Path(__file__).resolve().parent.parent / "alembic.ini"
_PREVIOUS_HEAD = "b8d1e4a70c93"
_LEGACY_KEY = "workflow:ship.build:auto_dispatch_on_plan_approval"
_PLAN_GATE_KEY = "workflow:ship.build:plan_gate"


def _config() -> Config:
    config = Config(str(_ALEMBIC_INI))
    config.set_main_option("sqlalchemy.url", TEST_DATABASE_URL)
    return config


def _reset_schema(connection) -> None:
    connection.exec_driver_sql("DROP SCHEMA IF EXISTS public CASCADE")
    connection.exec_driver_sql("CREATE SCHEMA public")


@pytest.mark.parametrize(
    ("legacy_value", "expected_value"),
    [(True, "machine"), (False, "human")],
)
def test_plan_gate_override_migrates_from_each_legacy_boolean(
    request, legacy_value, expected_value
):
    engine = create_engine(TEST_DATABASE_URL, isolation_level="AUTOCOMMIT")
    with engine.connect() as connection:
        _reset_schema(connection)
    try:
        command.upgrade(_config(), _PREVIOUS_HEAD)
        with engine.connect() as connection:
            connection.execute(
                text(
                    "INSERT INTO settings_overrides (key, value) "
                    "VALUES (:key, CAST(:value AS jsonb))"
                ),
                {"key": _LEGACY_KEY, "value": json.dumps(legacy_value)},
            )

        command.upgrade(_config(), "head")
        with engine.connect() as connection:
            result = connection.execute(
                text(
                    "SELECT key, value FROM settings_overrides "
                    "WHERE key IN (:legacy_key, :plan_gate_key)"
                ),
                {
                    "legacy_key": _LEGACY_KEY,
                    "plan_gate_key": _PLAN_GATE_KEY,
                },
            )
            rows = {row.key: row.value for row in result}

        assert rows == {_PLAN_GATE_KEY: expected_value}
    finally:
        with engine.connect() as connection:
            _reset_schema(connection)
        init_db(engine)
        engine.dispose()
        request.getfixturevalue("_druks_engine").dispose()

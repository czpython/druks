import json
import os
from pathlib import Path

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from druks.secrets import utils
from sqlalchemy import create_engine, text

# A fresh row encrypts under the current table name, so only a row sealed before
# the rename proves the migration reseals it: the envelope's AAD is
# "<table>.payload", and a renamed table opens nothing sealed under the old name.
# Runs the real platform history against its own database, so it opts out of
# the suite's rollback isolation (conftest ``_OWN_DATABASE_MODULES``).

PG_BASE = os.environ.get("DRUKS_TEST_PG", "postgresql://druks:druks@localhost:5432")
DB = "druks_migration_test"
URL = f"{PG_BASE.replace('postgresql://', 'postgresql+psycopg://')}/{DB}"
_ALEMBIC_INI = Path(__file__).resolve().parent.parent / "alembic.ini"
_BEFORE_RENAME = "b4c7e1a8d052"


def _pg_up() -> bool:
    try:
        psycopg.connect(f"{PG_BASE}/postgres", connect_timeout=2).close()
        return True
    except psycopg.Error:
        return False


pytestmark = pytest.mark.skipif(not _pg_up(), reason="test Postgres not reachable")


def _config() -> Config:
    config = Config(str(_ALEMBIC_INI))
    config.set_main_option("sqlalchemy.url", URL)
    return config


def test_renaming_the_logins_table_reseals_every_payload():
    admin = psycopg.connect(f"{PG_BASE}/postgres", autocommit=True)
    admin.execute(f"DROP DATABASE IF EXISTS {DB}")
    admin.execute(f"CREATE DATABASE {DB}")
    admin.close()
    command.upgrade(_config(), _BEFORE_RENAME)
    payload = {"access_token": "tok", "refresh_token": "ref"}
    engine = create_engine(URL)
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO accounts (id, username, created_at) VALUES ('acc', 'op', now())")
        )
        conn.execute(
            text(
                "INSERT INTO harness_logins "
                "(id, harness, account_id, provider_email, kind, payload, updated_at) "
                "VALUES ('login', 'claude', 'acc', 'op@example.com', 'subscription', "
                ":payload, now())"
            ).bindparams(
                payload=utils.encrypt(json.dumps(payload).encode(), "harness_logins.payload")
            )
        )

    command.upgrade(_config(), "head")

    with engine.connect() as conn:
        envelope = conn.execute(text("SELECT payload FROM provider_logins")).scalar_one()
    engine.dispose()
    assert json.loads(utils.decrypt(bytes(envelope), "provider_logins.payload")) == payload

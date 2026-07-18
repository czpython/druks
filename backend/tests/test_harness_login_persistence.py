import os

import psycopg
import pytest
from druks.database import configure_session, db_session, get_session
from druks.harnesses.claude import ClaudeHarness
from druks.harnesses.models import HarnessConnection
from sqlalchemy import create_engine, text

# The credential store's whole job is to persist a rotated credential dict
# through a real commit. The rollback-based suite can't verify that — its identity
# map hands back the mutated in-memory object no matter what reached the DB — so
# this module runs against its own database with real commits and fresh sessions,
# the way rotation actually persists in production.

PG_BASE = os.environ.get("DRUKS_TEST_PG", "postgresql://druks:druks@localhost:5432")
DB = "druks_harness_login_test"
URL = f"{PG_BASE.replace('postgresql://', 'postgresql+psycopg://')}/{DB}"


def _pg_up() -> bool:
    try:
        psycopg.connect(f"{PG_BASE}/postgres", connect_timeout=2).close()
        return True
    except psycopg.Error:
        return False


pytestmark = pytest.mark.skipif(not _pg_up(), reason="test Postgres not reachable")


@pytest.fixture
def engine():
    from druks.database import init_db

    admin = psycopg.connect(f"{PG_BASE}/postgres", autocommit=True)
    admin.execute(f"DROP DATABASE IF EXISTS {DB}")
    admin.execute(f"CREATE DATABASE {DB}")
    admin.close()

    created = create_engine(URL)
    init_db(created)
    configure_session(created)
    try:
        yield created
    finally:
        created.dispose()


def _committed(engine, work):
    session = get_session(engine)
    db_session.registry.set(session)
    try:
        result = work()
        session.commit()
        return result
    finally:
        db_session.remove()
        session.close()


def _connect(payload: dict) -> str:
    from druks.accounts.models import Account
    from druks.user_settings.models import UserSettings

    account = Account.get_or_create("op@example.com")
    settings = UserSettings.get()
    if not settings.fallback_account_id:
        settings.set_fallback_account(account.id)
    row = HarnessConnection.connect(
        harness="claude",
        account=account,
        payload=payload,
        expires_at=None,
        provider_email="op@example.com",
    )
    return row.id


def test_rotation_persists_new_payload_across_sessions(engine):
    # Connect, then rotate the payload the way rotate_token does (plain-dict
    # copy, edit, whole-value update), then read it back from a fresh session —
    # commit + new session proves the edit reached the DB, not just the
    # in-memory object.
    connection_id = _committed(
        engine, lambda: _connect({"claudeAiOauth": {"accessToken": "old", "refreshToken": "R0"}})
    )

    def rotate_in_place():
        row = HarnessConnection.get(connection_id)
        data = dict(row.payload)
        data["claudeAiOauth"]["accessToken"] = "new"
        row.update_payload(data, expires_at=None)

    _committed(engine, rotate_in_place)

    block = _committed(
        engine, lambda: dict(HarnessConnection.get(connection_id).payload)["claudeAiOauth"]
    )
    assert block["accessToken"] == "new"


def test_payload_is_ciphertext_at_rest(engine):
    _committed(engine, lambda: _connect({"claudeAiOauth": {"accessToken": "supersecret"}}))

    with engine.connect() as connection:
        stored = connection.execute(text("SELECT payload FROM harness_logins")).scalar_one()
    raw = bytes(stored)
    assert b"supersecret" not in raw
    assert b"claudeAiOauth" not in raw

    block = _committed(engine, lambda: ClaudeHarness.get_credentials()["claudeAiOauth"])
    assert block["accessToken"] == "supersecret"

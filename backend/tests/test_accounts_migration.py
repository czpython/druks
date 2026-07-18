import json
import os
from pathlib import Path

import druks
import psycopg
import pytest
from alembic import command
from alembic.config import Config
from druks.secrets.exceptions import SecretDecryptError
from druks.secrets.utils import decrypt
from sqlalchemy import create_engine, text

# The re-key migration is the one shot at existing credentials: it must fail
# closed without the operator identity, attach every legacy login when given
# one, and produce ciphertexts the ORM can read under the final AAD. That only
# proves out against a real Postgres walked from the previous head — so this
# module owns its database and drives Alembic directly.

PG_BASE = os.environ.get("DRUKS_TEST_PG", "postgresql://druks:druks@localhost:5432")
DB = "druks_accounts_migration_test"
URL = f"{PG_BASE.replace('postgresql://', 'postgresql+psycopg://')}/{DB}"

PREVIOUS_HEAD = "619e34746ef3"
_ALEMBIC_INI = Path(druks.__file__).resolve().parent.parent / "alembic.ini"

CLAUDE_PAYLOAD = {"claudeAiOauth": {"accessToken": "A0", "refreshToken": "R0"}}
CODEX_PAYLOAD = {"tokens": {"access_token": "T0", "refresh_token": "R0"}}


def _pg_up() -> bool:
    try:
        psycopg.connect(f"{PG_BASE}/postgres", connect_timeout=2).close()
        return True
    except psycopg.Error:
        return False


pytestmark = pytest.mark.skipif(not _pg_up(), reason="test Postgres not reachable")


def _upgrade(revision: str) -> None:
    config = Config(str(_ALEMBIC_INI))
    config.set_main_option("sqlalchemy.url", URL)
    command.upgrade(config, revision)


@pytest.fixture
def engine():
    admin = psycopg.connect(f"{PG_BASE}/postgres", autocommit=True)
    admin.execute(f"DROP DATABASE IF EXISTS {DB}")
    admin.execute(f"CREATE DATABASE {DB}")
    admin.close()

    _upgrade(PREVIOUS_HEAD)
    created = create_engine(URL)
    try:
        yield created
    finally:
        created.dispose()


def _seed_legacy(engine) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO harness_logins (harness, kind, payload, expires_at, account, "
                "updated_at) VALUES (:harness, 'subscription', CAST(:payload AS json), NULL, "
                ":account, now())"
            ),
            [
                {
                    "harness": "claude",
                    "payload": json.dumps(CLAUDE_PAYLOAD),
                    # Padding the migration strips; case is citext's to handle.
                    "account": " Legacy@Example.COM ",
                },
                {
                    # A legacy row that never recorded a provider email.
                    "harness": "codex",
                    "payload": json.dumps(CODEX_PAYLOAD),
                    "account": None,
                },
            ],
        )


def _rows(engine, query: str) -> list:
    with engine.connect() as connection:
        return connection.execute(text(query)).mappings().all()


def test_missing_dashboard_email_fails_before_data_mutation(engine, monkeypatch):
    _seed_legacy(engine)
    monkeypatch.delenv("DRUKS_DASHBOARD_EMAIL", raising=False)

    with pytest.raises(Exception, match="DRUKS_DASHBOARD_EMAIL"):
        _upgrade("head")

    # Nothing mutated: the old shape survives intact, no accounts table exists.
    legacy = _rows(engine, "SELECT harness, payload, account FROM harness_logins ORDER BY harness")
    assert [row["harness"] for row in legacy] == ["claude", "codex"]
    assert legacy[0]["payload"] == CLAUDE_PAYLOAD
    assert legacy[0]["account"] == " Legacy@Example.COM "
    tables = _rows(
        engine, "SELECT table_name FROM information_schema.tables WHERE table_name = 'accounts'"
    )
    assert tables == []


def test_blank_dashboard_email_fails_too(engine, monkeypatch):
    _seed_legacy(engine)
    monkeypatch.setenv("DRUKS_DASHBOARD_EMAIL", "   ")

    with pytest.raises(Exception, match="DRUKS_DASHBOARD_EMAIL"):
        _upgrade("head")


def test_legacy_rows_are_rekeyed_under_one_account(engine, monkeypatch):
    _seed_legacy(engine)
    monkeypatch.setenv("DRUKS_DASHBOARD_EMAIL", " Op@Example.COM ")

    _upgrade("head")

    accounts = _rows(engine, "SELECT id, username FROM accounts WHERE id != 'system'")
    assert len(accounts) == 1
    # Stored stripped, original case; citext matches it regardless of case.
    assert accounts[0]["username"] == "Op@Example.COM"
    assert _rows(engine, "SELECT id FROM accounts WHERE username = 'op@example.com'")

    logins = _rows(
        engine,
        "SELECT id, harness, account_id, provider_email, payload "
        "FROM harness_logins ORDER BY harness",
    )
    assert [row["harness"] for row in logins] == ["claude", "codex"]
    assert len({row["id"] for row in logins}) == 2
    for row in logins:
        # Every legacy login attaches to the one account, whatever its
        # provider email was.
        assert row["account_id"] == accounts[0]["id"]
    # That account is the execution fallback.
    fallback = _rows(engine, "SELECT fallback_account_id FROM user_settings")
    assert fallback[0]["fallback_account_id"] == accounts[0]["id"]
    assert logins[0]["provider_email"] == "Legacy@Example.COM"  # stripped, original case
    assert logins[1]["provider_email"] is None

    # Payloads decrypt under the final AAD only — an envelope minted against
    # the temporary column name would brick at the rename.
    originals = {"claude": CLAUDE_PAYLOAD, "codex": CODEX_PAYLOAD}
    for row in logins:
        envelope = bytes(row["payload"])
        assert json.loads(decrypt(envelope, "harness_logins.payload")) == originals[row["harness"]]
        with pytest.raises(SecretDecryptError):
            decrypt(envelope, "harness_logins.payload_ciphertext")


def test_orm_reads_migrated_rows_under_the_model_aad(engine, monkeypatch):
    _seed_legacy(engine)
    monkeypatch.setenv("DRUKS_DASHBOARD_EMAIL", "op@example.com")
    _upgrade("head")

    from druks.accounts.models import Account
    from druks.database import configure_session, db_session, get_session
    from druks.harnesses.claude import ClaudeHarness
    from druks.harnesses.models import HarnessConnection

    configure_session(engine)
    session = get_session(engine)
    db_session.registry.set(session)
    try:
        assert ClaudeHarness.get_credentials() == CLAUDE_PAYLOAD
        codex_row = HarnessConnection.get_for_account(
            "codex", Account.get_for_username("op@example.com").id
        )
        assert dict(codex_row.payload) == CODEX_PAYLOAD
        assert codex_row.provider_email is None
    finally:
        db_session.remove()
        session.close()


def test_fresh_install_migrates_without_the_variable(engine, monkeypatch):
    monkeypatch.delenv("DRUKS_DASHBOARD_EMAIL", raising=False)

    _upgrade("head")

    assert _rows(engine, "SELECT id FROM accounts") == [{"id": "system"}]
    assert _rows(engine, "SELECT id FROM harness_logins") == []

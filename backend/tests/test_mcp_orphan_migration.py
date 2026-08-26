"""Regression coverage for the ``b8f2c1a4d7e9`` orphaned-Linear-overlay cleanup.

This module owns its scratch database: it rebuilds the public schema to the
revision before the cleanup through the real platform Alembic env, seeds rows
with raw SQL, runs the migration, and asserts against the committed result. It is
registered with the own-database modules in ``conftest.py`` and restores the
shared create_all schema on teardown.
"""

from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from druks.core.models import uuid7_str
from druks.database import (
    create_async_engine_from_url,
    create_engine_from_url,
    session_scope,
)
from druks.mcp.exceptions import MissingTokenError
from druks.testing import TEST_DATABASE_URL, init_db
from druks.workspaces import Workspace
from sqlalchemy import create_engine

_ALEMBIC_INI = Path(__file__).resolve().parent.parent / "alembic.ini"
# The head just before the cleanup, and the cleanup itself.
_PRIOR_HEAD = "a4b9d3e17c62"
_ORPHAN_CLEANUP = "b8f2c1a4d7e9"
_LINEAR_URL = "https://mcp.linear.app/mcp"


class _FakeSandbox:
    ssh_username = "exedev"


def _config() -> Config:
    config = Config(str(_ALEMBIC_INI))
    config.set_main_option("sqlalchemy.url", TEST_DATABASE_URL)
    return config


@pytest.fixture
def at_prior_head():
    """Rebuild the public schema at the revision before the cleanup, hand back an
    AUTOCOMMIT engine to seed and assert with, then restore the shared create_all
    schema the rest of the suite runs against."""
    engine = create_engine(TEST_DATABASE_URL, isolation_level="AUTOCOMMIT")
    with engine.connect() as conn:
        conn.exec_driver_sql("DROP SCHEMA IF EXISTS public CASCADE")
        conn.exec_driver_sql("CREATE SCHEMA public")
    command.upgrade(_config(), _PRIOR_HEAD)
    try:
        yield engine
    finally:
        with engine.connect() as conn:
            conn.exec_driver_sql("DROP SCHEMA IF EXISTS public CASCADE")
            conn.exec_driver_sql("CREATE SCHEMA public")
        engine.dispose()
        # Restore exactly what the session schema fixture builds, so any later
        # module that reuses the shared schema finds it intact.
        init_db(create_engine_from_url(TEST_DATABASE_URL))


# --- raw-SQL seeding at the prior head -----------------------------------


def _seed_account(engine) -> str:
    account_id = uuid7_str()
    with engine.connect() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO accounts (id, username, created_at) VALUES (:id, :username, now())"
            ),
            {"id": account_id, "username": f"op-{account_id}"},
        )
    return account_id


def _seed_server(
    engine,
    *,
    name: str,
    token: bytes,
    token_source: str = "static",
    is_enabled: bool = True,
) -> str:
    server_id = uuid7_str()
    with engine.connect() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO mcp_servers"
                " (id, name, url, token, token_source, headers, secret_headers,"
                "  is_enabled, created_at, identity_mode)"
                " VALUES (:id, :name, :url, :token, :token_source, '{}'::jsonb,"
                "  ''::bytea, :is_enabled, now(), NULL)"
            ),
            {
                "id": server_id,
                "name": name,
                "url": _LINEAR_URL,
                "token": token,
                "token_source": token_source,
                "is_enabled": is_enabled,
            },
        )
    return server_id


def _seed_registration(engine, server_id: str, account_id: str) -> str:
    registration_id = uuid7_str()
    with engine.connect() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO mcp_client_registrations"
                " (id, server_id, account_id, token_endpoint, client_id, client_secret)"
                " VALUES (:id, :server_id, :account_id, :token_endpoint, :client_id,"
                "  ''::bytea)"
            ),
            {
                "id": registration_id,
                "server_id": server_id,
                "account_id": account_id,
                "token_endpoint": "https://mcp.linear.app/token",
                "client_id": "client-abc",
            },
        )
    return registration_id


def _seed_oauth_connection(engine, account_id: str, *, provider: str) -> str:
    connection_id = uuid7_str()
    with engine.connect() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO oauth_connections"
                " (id, provider, account_id, refresh_token, scopes, identity,"
                "  connected_at, revoked_at, revoked_reason)"
                " VALUES (:id, :provider, :account_id, :refresh_token, '[]'::jsonb,"
                "  '{}'::jsonb, now(), NULL, '')"
            ),
            {
                "id": connection_id,
                "provider": provider,
                "account_id": account_id,
                "refresh_token": b"refresh-secret",
            },
        )
    return connection_id


# --- read helpers --------------------------------------------------------


def _server_count(engine, name: str) -> int:
    with engine.connect() as conn:
        return conn.execute(
            sa.text("SELECT count(*) FROM mcp_servers WHERE name = :name"), {"name": name}
        ).scalar_one()


def _total_server_count(engine) -> int:
    with engine.connect() as conn:
        return conn.execute(sa.text("SELECT count(*) FROM mcp_servers")).scalar_one()


def _registration_count(engine, server_id: str) -> int:
    with engine.connect() as conn:
        return conn.execute(
            sa.text("SELECT count(*) FROM mcp_client_registrations WHERE server_id = :id"),
            {"id": server_id},
        ).scalar_one()


def _oauth_row(engine, connection_id: str) -> dict:
    with engine.connect() as conn:
        return dict(
            conn.execute(
                sa.text(
                    "SELECT revoked_at, revoked_reason, refresh_token"
                    " FROM oauth_connections WHERE id = :id"
                ),
                {"id": connection_id},
            )
            .mappings()
            .one()
        )


async def _deliver_mcp_servers() -> dict:
    # Exercise the real delivery seam against the committed DB, outside the
    # suite's rollback session (this module owns its database).
    engine = create_async_engine_from_url(TEST_DATABASE_URL)
    try:
        async with session_scope(engine):
            return await Workspace(sandbox=_FakeSandbox()).with_mcp_servers(None)  # type: ignore[arg-type]
    finally:
        await engine.dispose()


# --- the migration ------------------------------------------------------


async def test_orphan_overlay_is_removed_and_delivery_recovers(at_prior_head):
    engine = at_prior_head
    account_id = _seed_account(engine)
    server_id = _seed_server(engine, name="linear", token=b"", token_source="static")
    _seed_registration(engine, server_id, account_id)
    connection_id = _seed_oauth_connection(engine, account_id, provider="mcp:linear")

    # Before the cleanup the orphan is an enabled, tokenless, static custom
    # server — delivery fails loudly.
    with pytest.raises(MissingTokenError, match="linear"):
        await _deliver_mcp_servers()

    command.upgrade(_config(), _ORPHAN_CLEANUP)

    # The overlay row is gone; its client registration went with it through the
    # server-row cascade.
    assert _server_count(engine, "linear") == 0
    assert _registration_count(engine, server_id) == 0
    # The OAuth connection survives as a revoked audit record, its refresh token
    # cleared.
    row = _oauth_row(engine, connection_id)
    assert row["revoked_at"] is not None
    assert row["revoked_reason"] == "server_removed"
    assert bytes(row["refresh_token"]) == b""

    # Delivery no longer treats the orphan as an enabled tokenless server: it
    # neither raises nor ships any MCP server.
    kwargs = await _deliver_mcp_servers()
    assert "mcp_servers" not in kwargs


def test_cleanup_is_a_noop_on_a_fresh_install(at_prior_head):
    engine = at_prior_head
    assert _total_server_count(engine) == 0

    command.upgrade(_config(), _ORPHAN_CLEANUP)

    assert _total_server_count(engine) == 0


def test_cleanup_preserves_rows_that_do_not_match_the_signature(at_prior_head):
    engine = at_prior_head
    account_id = _seed_account(engine)
    # A different custom server: same tokenless static shape, but not named
    # ``linear`` — the signature is scoped by name, so it must survive.
    _seed_server(engine, name="figma", token=b"", token_source="static")
    # A legitimate custom ``linear`` server that only shares the name: it carries
    # a real static token, so it is not the orphan and must survive.
    _seed_server(engine, name="linear", token=b"real-token-ciphertext", token_source="static")
    # Its OAuth connection must stay live — nothing revoked it.
    connection_id = _seed_oauth_connection(engine, account_id, provider="mcp:linear")

    command.upgrade(_config(), _ORPHAN_CLEANUP)

    assert _server_count(engine, "figma") == 1
    assert _server_count(engine, "linear") == 1
    row = _oauth_row(engine, connection_id)
    assert row["revoked_at"] is None
    assert row["revoked_reason"] == ""
    assert bytes(row["refresh_token"]) == b"refresh-secret"

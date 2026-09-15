import os

import httpx
import psycopg
import pytest
from druks.accounts.models import Account
from druks.database import configure_session, get_session
from druks.db import db_session
from druks.secrets.datastructures import Audience
from druks.secrets.enums import SecretKind
from druks.secrets.models import VaultSecret
from druks.services import OauthClient, OauthRefreshError
from druks.testing import init_db
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import create_async_engine

# The subscription store's whole job is to persist a rotated subscription dict
# through a real commit. The rollback-based suite can't verify that — its identity
# map hands back the mutated in-memory object no matter what reached the DB — so
# this module runs against its own database with real commits and fresh sessions,
# the way rotation actually persists in production.

PG_BASE = os.environ.get("DRUKS_TEST_PG", "postgresql://druks:druks@localhost:5432")
DB = "druks_credential_test"
URL = f"{PG_BASE.replace('postgresql://', 'postgresql+psycopg://')}/{DB}"


def _pg_up() -> bool:
    try:
        psycopg.connect(f"{PG_BASE}/postgres", connect_timeout=2).close()
        return True
    except psycopg.Error:
        return False


pytestmark = pytest.mark.skipif(not _pg_up(), reason="test Postgres not reachable")


@pytest.fixture
async def engine():
    admin = psycopg.connect(f"{PG_BASE}/postgres", autocommit=True)
    admin.execute(f"DROP DATABASE IF EXISTS {DB}")
    admin.execute(f"CREATE DATABASE {DB}")
    admin.close()

    schema_engine = create_engine(URL)
    init_db(schema_engine)
    schema_engine.dispose()
    created = create_async_engine(URL)
    configure_session(created)
    try:
        yield created
    finally:
        await created.dispose()


async def _committed(engine, work):
    session = get_session(engine)
    db_session.registry.set(session)
    try:
        result = await work()
        await session.commit()
        return result
    finally:
        await db_session.remove()
        await session.close()


async def _connect(payload: dict) -> str:
    account = await Account.get_or_create(db_session(), "op@example.com")
    row = await VaultSecret.store(
        db_session(),
        SecretKind.SUBSCRIPTION,
        Audience.provider("anthropic"),
        account_id=account.id,
        secrets=payload,
        identity={"email": "op@example.com"},
        expires_at=None,
    )
    return row.id


async def test_reconnect_overwrites_the_payload_on_the_same_row(engine):
    async def connect_first():
        return await _connect({"claudeAiOauth": {"accessToken": "first"}})

    connection_id = await _committed(engine, connect_first)

    async def connect_again():
        return await _connect({"claudeAiOauth": {"accessToken": "second"}})

    reconnected_id = await _committed(engine, connect_again)

    async def read_back():
        row = await db_session().get(VaultSecret, connection_id)
        return dict(row.secrets)["claudeAiOauth"]["accessToken"]

    assert reconnected_id == connection_id
    assert await _committed(engine, read_back) == "second"


async def test_rotation_persists_new_payload_across_sessions(engine):
    # Connect, then rotate the payload the way rotate_token does (plain-dict
    # copy, edit, whole-value update), then read it back from a fresh session —
    # commit + new session proves the edit reached the DB, not just the
    # in-memory object.
    async def connect_old():
        return await _connect({"claudeAiOauth": {"accessToken": "old", "refreshToken": "R0"}})

    connection_id = await _committed(engine, connect_old)

    async def rotate_in_place():
        row = await db_session().get(VaultSecret, connection_id)
        data = dict(row.secrets)
        data["claudeAiOauth"]["accessToken"] = "new"
        await row.update_secrets(data, expires_at=None)

    await _committed(engine, rotate_in_place)

    async def read_back():
        row = await db_session().get(VaultSecret, connection_id)
        return dict(row.secrets)["claudeAiOauth"]

    block = await _committed(engine, read_back)
    assert block["accessToken"] == "new"


async def test_payload_is_ciphertext_at_rest(engine):
    async def connect_secret():
        return await _connect({"claudeAiOauth": {"accessToken": "supersecret"}})

    await _committed(engine, connect_secret)

    async with engine.connect() as connection:
        stored = (await connection.execute(text("SELECT secrets FROM vault"))).scalar_one()
    raw = bytes(stored)
    assert b"supersecret" not in raw
    assert b"claudeAiOauth" not in raw

    async def read_logins():
        row = await VaultSecret.lookup(
            db_session(),
            SecretKind.SUBSCRIPTION,
            Audience.provider("anthropic"),
            (await Account.get_default(db_session())).id,
        )
        return dict(row.secrets)["claudeAiOauth"]

    block = await _committed(engine, read_logins)
    assert block["accessToken"] == "supersecret"


async def test_invalid_grant_refresh_revokes_past_the_callers_rollback(engine, monkeypatch):
    # The refresh runs inside a durable step whose session rolls back when the
    # error propagates. The revoke commits on its own, so it outlives that rollback.
    def dead_grant(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "invalid_grant"})

    monkeypatch.setattr(
        "druks.services.oauth._http",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(dead_grant)),
    )
    published = []

    async def record(name, **kwargs):
        published.append((name, kwargs))

    monkeypatch.setattr("druks.services.oauth.publish", record)
    client = OauthClient(
        provider="acme",
        authorization_endpoint="https://auth.acme.test/authorize",
        token_endpoint="https://auth.acme.test/token",
        client_id="client-123",
        client_secret="secret-123",
    )

    async def connect():
        row = await VaultSecret.connect(
            db_session(),
            Audience.service("acme"),
            account_id=None,
            refresh_token="rt-old",
            scopes=[],
        )
        return row.id

    connection_id = await _committed(engine, connect)

    session = get_session(engine)
    db_session.registry.set(session)
    try:
        connection = await db_session().get(VaultSecret, connection_id)
        with pytest.raises(OauthRefreshError, match="sign in again"):
            await client.get_access_token(db_session(), connection=connection)
        await session.rollback()
    finally:
        await db_session.remove()
        await session.close()

    async def read_back():
        row = await db_session().get(VaultSecret, connection_id)
        return row.revoked_reason, dict(row.secrets)

    assert await _committed(engine, read_back) == ("invalid_grant", {})
    assert published == [
        (
            "oauth.disconnected",
            {"provider": "acme", "connection_id": connection_id, "account_id": None},
        )
    ]

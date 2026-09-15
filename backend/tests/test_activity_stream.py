import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import psycopg
import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from druks.database import session_scope
from druks.events import routes
from druks.events.models import Event
from druks.testing import TEST_DATABASE_URL, init_db
from druks_field_notes.models import Note
from fastapi import HTTPException
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine


@pytest.fixture
async def activity_engine(druks_db):
    database = make_url(TEST_DATABASE_URL).set(database="druks_activity_test")
    admin_url = database.set(drivername="postgresql", database="postgres")
    with psycopg.connect(admin_url.render_as_string(hide_password=False), autocommit=True) as admin:
        admin.execute("DROP DATABASE IF EXISTS druks_activity_test")
        admin.execute("CREATE DATABASE druks_activity_test")
    schema_engine = create_engine(database)
    init_db(schema_engine)
    schema_engine.dispose()
    engine = create_async_engine(database)
    try:
        yield engine
    finally:
        await engine.dispose()
        with psycopg.connect(
            admin_url.render_as_string(hide_password=False), autocommit=True
        ) as admin:
            admin.execute("DROP DATABASE druks_activity_test")


@pytest.fixture
def open_stream(activity_engine, monkeypatch):
    monkeypatch.setattr(routes.asyncio, "sleep", AsyncMock())

    async def stream(*, after=None, headers=None, **filters):
        request = SimpleNamespace(
            headers=headers or {}, is_disconnected=AsyncMock(return_value=False)
        )
        response = await routes.stream_feed(
            request=request, engine=activity_engine, after=after, **filters
        )
        return response.body_iterator

    return stream


async def read_batch(stream):
    items = []
    while True:
        message = await anext(stream)
        fields = dict(line.split(": ", 1) for line in message.splitlines() if line)
        data = json.loads(fields["data"])
        if fields.get("event") == "batch-end":
            assert fields["id"] == data["cursor"]
            return items, data["cursor"]
        assert "id" not in fields
        items.append(data)


@pytest.mark.parametrize("resume", ["connected", "after", "header"])
async def test_late_commit_arrives_after_a_higher_sequence(activity_engine, open_stream, resume):
    async with session_scope(activity_engine) as session:
        first = await routes.list_feed(session=session)
    assert first.items == []
    assert first.stream_cursor

    stream = await open_stream(after=first.stream_cursor, app="field_notes", topic="merged")
    try:
        async with session_scope(activity_engine) as slow:
            slow_pid = await slow.scalar(text("SELECT pg_backend_pid()"))
            late = await Note.create(body="Late commit")
            await late.announce("merged")
            late_sequence = await slow.scalar(select(Event.id))

            async with session_scope(activity_engine) as fast:
                assert await fast.scalar(text("SELECT pg_backend_pid()")) != slow_pid
                early = await Note.create(body="Early commit")
                await early.announce("merged")
                early_sequence = await fast.scalar(select(Event.id))

            assert late_sequence < early_sequence
            items, cursor = await read_batch(stream)
            assert [item["seq"] for item in items] == [early_sequence]

        if resume != "connected":
            await stream.aclose()
            stream = await open_stream(
                after=cursor if resume == "after" else first.stream_cursor,
                headers={"last-event-id": cursor} if resume == "header" else {},
                app="field_notes",
                topic="merged",
            )
        items, _ = await read_batch(stream)
        assert [item["seq"] for item in items] == [late_sequence]
        assert (await read_batch(stream))[0] == []
    finally:
        await stream.aclose()


@pytest.mark.parametrize("resume", ["after", "header"])
async def test_an_interrupted_batch_replays_all_rows_before_its_cursor(
    activity_engine, open_stream, resume
):
    async with session_scope(activity_engine) as session:
        first = await routes.list_feed(session=session)
    async with session_scope(activity_engine) as session:
        note = await Note.create(body="Batch")
        for number in range(125):
            await note.announce("summary.ready", summary=str(number))
        expected = list(await session.scalars(select(Event.id).order_by(Event.id)))

    stream = await open_stream(after=first.stream_cursor)
    received = []
    for _ in range(2):
        message = await anext(stream)
        assert message.startswith("data: ")
        received.append(json.loads(message.removeprefix("data: ")))
    await stream.aclose()

    stream = await open_stream(
        after=first.stream_cursor if resume == "after" else None,
        headers={"last-event-id": first.stream_cursor} if resume == "header" else {},
    )
    try:
        items, _ = await read_batch(stream)
        assert [item["seq"] for item in items] == expected
        assert len({item["id"] for item in [*received, *items]}) == 125
        assert (await read_batch(stream))[0] == []
    finally:
        await stream.aclose()


async def test_history_snapshot_precedes_a_commit_after_its_read(
    activity_engine, open_stream, monkeypatch
):
    async with session_scope(activity_engine) as session:
        execute = session.execute

        async def read_then_announce(statement):
            result = await execute(statement)
            async with session_scope(activity_engine):
                note = await Note.create(body="After the history read")
                await note.announce("summary.ready", summary="New finding")
            return result

        monkeypatch.setattr(session, "execute", read_then_announce)
        first = await routes.list_feed(session=session)

    assert first.items == []
    stream = await open_stream(after=first.stream_cursor)
    try:
        items, _ = await read_batch(stream)
        assert [item["summary"] for item in items] == ["New finding"]
    finally:
        await stream.aclose()


@pytest.mark.parametrize("cursor", ["42", "invalid", "20:10:", "10:20:99"])
async def test_invalid_snapshots_fail_before_streaming(open_stream, cursor):
    with pytest.raises(HTTPException, match="Invalid Activity snapshot") as raised:
        await open_stream(after=cursor)
    assert raised.value.status_code == 400


async def test_event_transaction_migration_preserves_history(druks_db):
    note = await Note.create(body="Recorded before the migration")
    await note.announce("summary.ready")
    before = (await druks_db.execute(select(Event.id, Event.payload))).all()
    migration = (
        Path(__file__).resolve().parent.parent
        / "migrations/versions/8b194c60e72a_event_transaction_visibility.py"
    )
    spec = importlib.util.spec_from_file_location("event_transaction_visibility", migration)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    def migrate(connection):
        with Operations.context(MigrationContext.configure(connection)):
            module.downgrade()
            module.upgrade()

    await (await druks_db.connection()).run_sync(migrate)
    assert (await druks_db.execute(select(Event.id, Event.payload))).all() == before
    assert await druks_db.scalar(text("SELECT bool_and(xid = pg_current_xact_id()) FROM events"))
    assert await druks_db.scalar(text("SELECT to_regclass('events_xid_idx')"))

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from conftest import installation_key
from druks.accounts.dependencies import current_account
from druks.api.server import app
from druks.database import db_session
from druks.durable.models import AgentCall, Artifact
from druks.events import routes
from druks.events.builder import build_feed
from druks.events.models import Event
from druks.testing import seed_run
from druks_field_notes.models import Note
from druks_field_notes.workflows import Summarize
from fastapi import HTTPException
from sqlalchemy import event as sqlalchemy_event


@pytest.fixture
async def history(druks_db):
    db_session.registry.set(druks_db)
    start = datetime(2026, 9, 9, tzinfo=UTC)
    rows = [
        Event(
            type="summary.ready", app="field_notes", subject_label="ACME%_ One", created_at=start
        ),
        Event(
            type="summary.ready",
            app="field_notes",
            subject_label="acme%_ Two",
            created_at=start + timedelta(hours=1),
        ),
        Event(
            type="plan.prepared",
            app="software_factory",
            subject_label="ACMEZZ Two",
            created_at=start + timedelta(hours=2),
        ),
        Event(
            type="later.kind",
            app="field_notes",
            subject_label="Unrelated",
            created_at=start + timedelta(days=1),
        ),
        Event(type="hidden", app="core", subject_label="ACME%_ Core"),
        Event(type="hidden", app="usage", subject_label="ACME%_ Usage"),
        Event(type="hidden", app="not_installed", subject_label="ACME%_ Other"),
        Event(type="hidden", subject_label="ACME%_ Unowned"),
    ]
    druks_db.add_all(rows)
    await druks_db.flush()
    return rows


async def test_literal_search_and_half_open_dates(druks_client, history):
    response = await druks_client.get(
        "/api/events",
        params={
            "q": "  aCmE%_  ",
            "app": "field_notes",
            "kind": "summary.ready",
            "from": "2026-09-09T00:00:00Z",
            "until": "2026-09-09T01:00:00Z",
        },
    )
    assert response.status_code == 200
    data = response.json()
    assert [item["seq"] for item in data["items"]] == [history[0].id]
    assert data["kinds"] == ["later.kind", "summary.ready"]
    blank = (await druks_client.get("/api/events", params={"q": "   "})).json()
    assert [item["seq"] for item in blank["items"]] == [row.id for row in reversed(history[:4])]
    assert blank["kinds"] == ["later.kind", "plan.prepared", "summary.ready"]


@pytest.mark.parametrize(
    "params",
    [
        {"from": "2026-09-10T00:00:00Z", "until": "2026-09-09T00:00:00Z"},
        {"from": "2026-09-09T00:00:00Z", "until": "2026-09-09T00:00:00Z"},
        {"from": "2026-09-09T00:00:00"},
        {"until": "not-a-date"},
    ],
)
async def test_invalid_date_bounds_fail_validation(druks_client, params):
    assert (await druks_client.get("/api/events", params=params)).status_code == 422


async def test_kinds_only_query_the_first_page(druks_db, druks_client, history):
    statements = []

    def record(_connection, _cursor, statement, _parameters, _context, _many):
        if "SELECT DISTINCT" in statement and "events.type" in statement:
            statements.append(statement)

    engine = druks_db.bind.sync_engine
    sqlalchemy_event.listen(engine, "before_cursor_execute", record)
    try:
        first = (await druks_client.get("/api/events", params={"limit": 1})).json()
        assert first["kinds"] == ["later.kind", "plan.prepared", "summary.ready"]
        assert len(statements) == 1
        second = (
            await druks_client.get(
                "/api/events", params={"limit": 1, "before": first["nextCursor"]}
            )
        ).json()
        assert "kinds" not in second
        await build_feed(app="field_notes")
        assert len(statements) == 1
        hidden = (await druks_client.get("/api/events", params={"app": "not_installed"})).json()
        assert hidden["items"] == hidden["kinds"] == []
    finally:
        sqlalchemy_event.remove(engine, "before_cursor_execute", record)


async def test_routine_lifecycle_is_not_activity(druks_db):
    db_session.registry.set(druks_db)
    for kind in ["workflow.running", "workflow.finished", "workflow.step", "workflow.retry"]:
        await Event.emit(type=kind, app="field_notes", payload={"run": "gone"})
    await Event.emit(
        type="workflow.running", app="field_notes", payload={"gate": "review", "result": {}}
    )
    await Event.emit(
        type="workflow.running",
        app="field_notes",
        payload={
            "run": "gone",
            "gate": "review",
            "input_requested_at": "2026-09-09T01:00:00Z",
            "result": {"action": "approve"},
        },
    )
    items, _ = await build_feed()
    assert len(items) == 1
    assert items[0].run == "gone"
    assert items[0].gate == "review"
    assert items[0].parked_at == datetime(2026, 9, 9, 1, tzinfo=UTC)
    assert not items[0].is_run_available


async def test_exact_artifact_and_recorded_label_survive_later_results_and_deletion(
    druks_db, tmp_path, monkeypatch
):
    db_session.registry.set(druks_db)
    monkeypatch.setenv("DRUKS_DATA_DIR", str(tmp_path))
    note = await Note.create(body="Original work")
    run = await seed_run(druks_db, kind=Summarize.kind, subject=note)
    key = await installation_key()
    calls = [
        AgentCall(
            id=f"history-{number}",
            run_id=run.id,
            agent="field_notes.summarize",
            model="test",
            sandbox_host_id="test",
            api_key_id=key.id,
        )
        for number in (1, 2)
    ]
    druks_db.add_all(calls)
    await druks_db.flush()
    druks_db.expunge_all()
    for call in calls:
        await Artifact.record(
            call_dir=call.call_dir,
            call_id=call.id,
            kind="markdown",
            title="Result",
            content=call.id,
            activity={"kind": "summary.ready"},
        )
    items, _ = await build_feed()
    assert [item.agent_call_id for item in items] == [calls[1].id, calls[0].id]
    assert all(
        item.is_artifact_available and item.is_run_available and item.is_subject_available
        for item in items
    )
    artifact = await Artifact.get_for_call(calls[0].id)
    assert items[1].artifact_id == artifact.id
    recorded_label = items[1].subject_label
    await druks_db.delete(artifact)
    await druks_db.delete(await druks_db.merge(note))
    await druks_db.flush()
    missing, _ = await build_feed()
    assert missing[1].subject_label == recorded_label
    assert missing[1].artifact_id == artifact.id
    assert not missing[1].is_artifact_available
    assert not missing[1].is_subject_available


async def test_search_does_not_read_payloads_or_current_subject_text(druks_db):
    db_session.registry.set(druks_db)
    note = await Note.create(body="Needle")
    await Event.emit(
        type="build.rejected",
        app="field_notes",
        subject=note.identity,
        label="Recorded work",
        payload={"reason": "Needle", "summary": "Needle"},
    )
    assert not (await build_feed(q="needle"))[0]
    items, _ = await build_feed(q="recorded")
    assert items[0].reason == "Needle"
    assert not items[0].run


async def test_reconnect_catches_up_all_pages_without_a_kinds_query(druks_db, monkeypatch):
    db_session.registry.set(druks_db)
    for number in range(205):
        await Event.emit(type="summary.ready", app="field_notes", label=str(number))
    first_page, _ = await build_feed(limit=205)
    cursor = first_page[-1].seq
    queries = []

    def record(_connection, _cursor, statement, _parameters, _context, _many):
        queries.append(statement)

    @asynccontextmanager
    async def scope(_engine):
        yield druks_db

    monkeypatch.setattr(routes, "session_scope", scope)
    monkeypatch.setattr(routes.asyncio, "sleep", AsyncMock())
    request = SimpleNamespace(
        headers={"last-event-id": str(cursor)}, is_disconnected=AsyncMock(side_effect=[False, True])
    )
    engine = druks_db.bind.sync_engine
    sqlalchemy_event.listen(engine, "before_cursor_execute", record)
    try:
        response = await routes.stream_feed(
            request=request, engine=None, filters={"app": "field_notes"}, after=None
        )
        messages = [message async for message in response.body_iterator]
    finally:
        sqlalchemy_event.remove(engine, "before_cursor_execute", record)
    sequences = [int(message.splitlines()[0].removeprefix("id: ")) for message in messages]
    assert sequences == sorted(item.seq for item in first_page if item.seq > cursor)
    assert len(sequences) == len(set(sequences)) == 204
    assert not any("SELECT DISTINCT" in query for query in queries)


async def test_activity_requires_the_existing_account_authorization(druks_client, history):
    original = app.dependency_overrides[current_account]

    async def unauthorized():
        raise HTTPException(401, "No operator identity")

    app.dependency_overrides[current_account] = unauthorized
    try:
        assert (await druks_client.get("/api/events")).status_code == 401
        assert (await druks_client.get("/api/events/stream")).status_code == 401
    finally:
        app.dependency_overrides[current_account] = original

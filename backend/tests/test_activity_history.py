from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from conftest import installation_key
from druks.database import db_session
from druks.durable.models import AgentCall, Artifact
from druks.events import routes
from druks.events.models import Event
from druks.testing import seed_run
from druks_field_notes.models import Note
from druks_field_notes.workflows import Summarize
from sqlalchemy import event as sqlalchemy_event
from sqlalchemy import func, select


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
            subject_label="acme/widget",
            created_at=start + timedelta(days=1),
        ),
        Event(type="hidden", app="core", subject_label="ACME%_ Core"),
        Event(type="hidden", app="usage", subject_label="ACME%_ Usage"),
        Event(type="hidden", app="not_installed", subject_label="ACME%_ Other"),
        Event(type="hidden", subject_label="ACME%_ Unowned"),
        Event(type="summary.ready", app="field_notes", created_at=start - timedelta(days=1)),
    ]
    druks_db.add_all(rows)
    await druks_db.flush()
    return rows


async def test_filters_match_literally_and_bound_dates(druks_client, history):
    async def read(params):
        response = await druks_client.get("/api/events", params=params)
        assert response.status_code == 200
        return [item["seq"] for item in response.json()["items"]]

    one, two, factory, widget, unlabelled = (row.id for row in [*history[:4], history[8]])
    combined = {
        "q": "  aCmE%_  ",
        "app": "field_notes",
        "kind": "summary.ready",
        "from": "2026-09-09T00:00:00Z",
        "until": "2026-09-09T01:00:00Z",
    }
    assert await read(combined) == [one]
    assert await read({"q": "aCmE%_"}) == [two, one]
    assert await read({"q": "ACME/WIDGET"}) == [widget]
    assert await read({"kind": "summary.ready"}) == [unlabelled, two, one]
    assert await read({"from": "2026-09-09T01:00:00Z"}) == [widget, factory, two]
    assert await read({"q": "   "}) == [unlabelled, widget, factory, two, one]


async def test_kinds_list_every_eligible_type_in_the_app_scope(druks_client, history):
    async def kinds(params):
        return (await druks_client.get("/api/events/kinds", params=params)).json()

    assert await kinds({}) == ["later.kind", "plan.prepared", "summary.ready"]
    assert await kinds({"app": "field_notes"}) == ["later.kind", "summary.ready"]
    assert await kinds({"app": "not_installed"}) == []


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


async def test_pages_follow_the_cursor(druks_client, history):
    async def page(params):
        return (await druks_client.get("/api/events", params=params)).json()

    first = await page({"limit": 1})
    assert [item["seq"] for item in first["items"]] == [history[8].id]
    second = await page({"limit": 1, "before": first["nextCursor"]})
    assert [item["seq"] for item in second["items"]] == [history[3].id]
    assert (await page({"limit": 4}))["nextCursor"] == str(history[1].id)
    assert (await page({"limit": 5}))["nextCursor"] is None
    hidden = await druks_client.get(f"/api/events/{history[4].id}/destinations")
    assert hidden.status_code == 404


async def test_activity_keeps_decisions_failures_and_stops(druks_db, druks_client):
    db_session.registry.set(druks_db)
    for kind in ["workflow.running", "workflow.finished", "workflow.step", "workflow.retry"]:
        await Event.emit(druks_db, type=kind, app="field_notes", payload={"run": "gone"})
    round_facts = {"run": "gone", "gate": "review", "input_requested_at": "2026-09-09T01:00:00Z"}
    await Event.emit(
        druks_db, type="workflow.scheduled", app="field_notes", payload={"run": "gone"}
    )
    await Event.emit(druks_db, type="workflow.parked", app="field_notes", payload=round_facts)
    await Event.emit(
        druks_db,
        type="workflow.running",
        app="field_notes",
        payload={**round_facts, "result": {"action": "approve"}},
    )
    await Event.emit(
        druks_db,
        type="workflow.failed",
        app="field_notes",
        payload={"run": "gone", "failure": "Timed out"},
    )
    await Event.emit(
        druks_db,
        type="workflow.cancelled",
        app="field_notes",
        payload={"run": "gone", "failure": "Stopped"},
    )
    items = (await druks_client.get("/api/events")).json()["items"]
    kinds = [
        "workflow.cancelled",
        "workflow.failed",
        "workflow.running",
        "workflow.parked",
        "workflow.scheduled",
    ]
    assert [item["kind"] for item in items] == kinds
    assert (await druks_client.get("/api/events/kinds")).json() == sorted(kinds)
    cancelled, failed, receipt = items[:3]
    assert (receipt["gate"], receipt["parkedAt"]) == ("review", "2026-09-09T01:00:00Z")
    assert (failed["failure"], cancelled["failure"]) == ("Timed out", "Stopped")


async def test_destinations_report_what_still_exists(druks_db, druks_client, tmp_path, monkeypatch):
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
            druks_db,
            call_dir=call.call_dir,
            call_id=call.id,
            kind="markdown",
            title="Result",
            content=call.id,
            event={"topic": "summary.ready"},
        )
    artifacts = [await Artifact.get_for_call(druks_db, call.id) for call in calls]
    items = (await druks_client.get("/api/events")).json()["items"]
    assert [item["artifactId"] for item in items] == [artifacts[1].id, artifacts[0].id]
    destinations = f"/api/events/{items[1]['seq']}/destinations"
    assert (await druks_client.get(destinations)).json() == {
        "isSubjectAvailable": True,
        "isRunAvailable": True,
        "isArtifactAvailable": True,
    }
    await druks_db.delete(artifacts[0])
    await druks_db.delete(await druks_db.merge(note))
    await druks_db.flush()
    assert (await druks_client.get(destinations)).json() == {
        "isSubjectAvailable": False,
        "isRunAvailable": True,
        "isArtifactAvailable": False,
    }
    recorded = (await druks_client.get("/api/events")).json()["items"][1]
    assert (recorded["subjectLabel"], recorded["artifactId"]) == (
        items[1]["subjectLabel"],
        artifacts[0].id,
    )


async def test_search_does_not_read_payloads_or_current_subject_text(druks_db, druks_client):
    db_session.registry.set(druks_db)
    note = await Note.create(body="Needle")
    await Event.emit(
        druks_db,
        type="build.rejected",
        app="field_notes",
        subject=note.identity,
        label="Recorded work",
        payload={"reason": "Needle", "summary": "Needle"},
    )

    async def search(text):
        return (await druks_client.get("/api/events", params={"q": text})).json()["items"]

    assert not await search("needle")
    [item] = await search("recorded")
    assert (item["reason"], item["run"]) == ("Needle", None)


@pytest.mark.parametrize(
    ("header_row", "after_row", "first_sent_row"),
    [(None, 0, 1), (2, 0, 3), (None, None, 3)],
)
async def test_stream_catches_up_in_pages_then_sends_new_rows_once(
    druks_db, monkeypatch, header_row, after_row, first_sent_row
):
    db_session.registry.set(druks_db)
    for number in range(5):
        await Event.emit(druks_db, type="summary.ready", app="field_notes", label=str(number))
        await Event.emit(druks_db, type="later.kind", app="field_notes", label=str(number))
    matching = list(
        await druks_db.scalars(
            select(Event.id).where(Event.type == "summary.ready").order_by(Event.id)
        )
    )

    async def record_a_new_row(_seconds):
        if len(matching) == 5:
            await Event.emit(druks_db, type="summary.ready", app="field_notes", label="new")
            matching.append(await druks_db.scalar(select(func.max(Event.id))))

    @asynccontextmanager
    async def scope(_engine):
        yield druks_db

    queries = []

    def record(_connection, _cursor, statement, _parameters, _context, _many):
        queries.append(statement)

    monkeypatch.setattr(routes, "session_scope", scope)
    monkeypatch.setattr(routes, "_SSE_PAGE_SIZE", 2)
    monkeypatch.setattr(routes.asyncio, "sleep", record_a_new_row)
    headers = {"last-event-id": str(matching[header_row])} if header_row is not None else {}
    after = str(matching[after_row]) if after_row is not None else None
    request = SimpleNamespace(
        headers=headers, is_disconnected=AsyncMock(side_effect=[False] * 5 + [True])
    )
    engine = druks_db.bind.sync_engine
    sqlalchemy_event.listen(engine, "before_cursor_execute", record)
    try:
        response = await routes.stream_feed(
            request=request, engine=None, app="field_notes", kind="summary.ready", after=after
        )
        messages = [message async for message in response.body_iterator]
    finally:
        sqlalchemy_event.remove(engine, "before_cursor_execute", record)
    sequences = [int(message.splitlines()[0].removeprefix("id: ")) for message in messages]
    assert sequences == matching[first_sent_row:]
    assert not any("SELECT DISTINCT" in query for query in queries)

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

from druks.database import db_session
from druks.events import routes
from druks_field_notes.models import Note, Repository


async def test_recorded_titles_and_facts_survive_rename_and_deletion(
    druks_db, druks_client, monkeypatch
):
    db_session.registry.set(druks_db)
    note = await Note.create(body="Pump 50%_ hot")
    assert note.get_summary().title == "Pump 50%_ hot"
    facts = {"revision_number": 2, "inspection": {"sensor_id": "A", "readings": [0, 50]}}
    await note.announce("note.inspected", title="Forged title", **facts)
    label = note.label
    note.body = "Renamed observation"
    await druks_db.flush()
    await druks_db.delete(note)
    await druks_db.flush()
    other = await Note.create(body="Pump 50ZZ hot")
    await other.announce("note.inspected", **facts)

    filters = {"app": "field_notes", "topic": "note.inspected", "q": "  pUMP 50%_  "}
    page = (await druks_client.get("/api/events", params={**filters, "limit": 1})).json()
    [recorded] = page["items"]
    assert page["nextCursor"] is None
    assert recorded["subjectLabel"] == label
    assert recorded["payload"] == {**facts, "title": "Pump 50%_ hot"}
    assert set(recorded) == {
        "id",
        "seq",
        "at",
        "topic",
        "app",
        "subjectType",
        "subjectId",
        "subjectLabel",
        "payload",
    }
    assert not (await druks_client.get("/api/events", params={"q": "renamed"})).json()["items"]
    assert (await druks_client.get("/api/events", params={"q": label.upper()})).json()["items"] == [
        recorded
    ]

    @asynccontextmanager
    async def scope(_engine):
        yield druks_db

    monkeypatch.setattr(routes, "session_scope", scope)
    monkeypatch.setattr(routes.asyncio, "sleep", AsyncMock())
    request = SimpleNamespace(headers={}, is_disconnected=AsyncMock(side_effect=[False, True]))
    response = await routes.stream_feed(
        request=request,
        engine=None,
        app=filters["app"],
        topic=filters["topic"],
        search=filters["q"],
    )
    streamed = [
        json.loads(message.removeprefix("data: "))
        async for message in response.body_iterator
        if message.startswith("data: ")
    ]
    assert streamed == [recorded]


async def test_a_summary_without_a_title_keeps_the_work_key_and_facts(druks_db, druks_client):
    db_session.registry.set(druks_db)
    repository = await Repository.create(repo="acme/observations")
    assert repository.get_summary().title is None
    await repository.announce("repository.inspected", title="Forged title", branch_name="main")

    [recorded] = (await druks_client.get("/api/events")).json()["items"]
    assert recorded["subjectLabel"] == "acme/observations"
    assert recorded["payload"] == {"branch_name": "main"}

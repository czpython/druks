from unittest.mock import AsyncMock

import pytest
from conftest import installation_key
from druks.database import db_session
from druks.durable.models import AgentCall, Artifact
from druks.events.models import Event
from druks.testing import seed_run
from druks_field_notes.models import Note
from druks_field_notes.workflows import Summarize
from sqlalchemy import select


@pytest.fixture
async def output_calls(druks_db):
    db_session.registry.set(druks_db)
    note = await Note.create(body="The reviewed work")
    run = await seed_run(druks_db, kind=Summarize.kind, subject=note)
    key = await installation_key()
    calls = [
        AgentCall(
            id=f"output-{number}",
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
    return note, run, calls


async def test_output_persistence_keeps_one_event_per_call(druks_db, output_calls, tmp_path):
    db_session.registry.set(druks_db)
    note, run, calls = output_calls
    for call in calls:
        for _ in range(2):
            await Artifact.record(
                call_dir=tmp_path / call.id,
                call_id=call.id,
                kind="markdown",
                title="Review",
                content="No unresolved findings.",
                activity={"kind": "review.completed", "summary": "No unresolved findings."},
            )
            await druks_db.commit()
    artifacts = list(await druks_db.scalars(select(Artifact).order_by(Artifact.agent_call_id)))
    events = list(await druks_db.scalars(select(Event).order_by(Event.id)))
    assert len(artifacts) == len(events) == 2
    assert {event.app for event in events} == {"field_notes"}
    assert {event.subject_id for event in events} == {str(note.id)}
    assert {event.subject_label for event in events} == {note.label}
    for call, artifact, event in zip(calls, artifacts, events, strict=True):
        assert event.type == "review.completed"
        assert event.payload == {
            "run": run.id,
            "kind": run.kind,
            "agent_call_id": call.id,
            "artifact_id": artifact.id,
            "summary": "No unresolved findings.",
        }


async def test_artifact_without_activity_creates_no_event(druks_db, output_calls, tmp_path):
    db_session.registry.set(druks_db)
    _, _, calls = output_calls
    await Artifact.record(
        call_dir=tmp_path,
        call_id=calls[0].id,
        kind="markdown",
        title="Internal result",
        content="A working note.",
    )
    assert await Artifact.get_for_call(calls[0].id)
    assert not list(await druks_db.scalars(select(Event)))


async def test_event_failure_rolls_back_the_artifact(druks_db, output_calls, tmp_path, monkeypatch):
    db_session.registry.set(druks_db)
    _, _, calls = output_calls
    call_id = calls[0].id
    with monkeypatch.context() as patched:
        patched.setattr(Event, "emit", AsyncMock(side_effect=RuntimeError("Event insert failed")))
        with pytest.raises(RuntimeError, match="Event insert failed"):
            async with druks_db.begin_nested():
                await Artifact.record(
                    call_dir=tmp_path,
                    call_id=call_id,
                    kind="markdown",
                    title="Review",
                    content="Reviewed.",
                    activity={"kind": "review.completed"},
                )
    assert not await Artifact.get_for_call(call_id)
    assert not list(await druks_db.scalars(select(Event)))

    await Artifact.record(
        call_dir=tmp_path,
        call_id=call_id,
        kind="markdown",
        title="Review",
        content="Reviewed.",
        activity={"kind": "review.completed"},
    )
    assert await Artifact.get_for_call(call_id)
    events = list(await druks_db.scalars(select(Event)))
    assert len(events) == 1
    assert "summary" not in events[0].payload

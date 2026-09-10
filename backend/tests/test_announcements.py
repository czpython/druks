import pytest
from druks.apps import loader
from druks.db import db_session
from druks.events.models import Event
from druks.signals import subscribe
from druks.workflows import Subject, WorkflowError
from druks_field_notes.models import Note
from sqlalchemy import select


class Report(Subject):
    pass


async def test_identity_subject_uses_its_registered_app(druks_db, monkeypatch):
    monkeypatch.setitem(loader._workflow_packages, __name__, "reports")
    report = Report(id="owner/repository#7")
    received = []

    @subscribe("report.published", subject=Report)
    async def receive(*, subject: Report, url: str) -> None:
        received.append((subject, url))

    await report.announce("report.published", url="https://example.com/report/7")

    event = (await druks_db.scalars(select(Event).filter_by(type="report.published"))).one()
    assert event.app == "reports"
    assert event.subject_type == "report"
    assert event.subject_id == report.id
    assert event.subject_label == report.label
    assert event.payload == {"url": "https://example.com/report/7"}
    assert received == [(report, "https://example.com/report/7")]


async def test_unregistered_subject_names_the_missing_registration(druks_db):
    with pytest.raises(WorkflowError, match="register_workflow_package"):
        await Report(id="owner/repository#7").announce("report.published")


async def test_stored_subject_announces_with_its_app(druks_db):
    note = await Note.create(body="An observation")

    await note.announce("note.revised", revision=2)

    event = (await druks_db.scalars(select(Event).filter_by(type="note.revised"))).one()
    assert event.app == "field_notes"
    assert event.subject_id == str(note.id)
    assert event.subject_label == note.label
    assert event.payload == {"revision": 2}


async def test_subject_delivery_error_rolls_back_with_the_domain_transaction(druks_db):
    # The savepoint below is the fixture session's, so the announce must run on it.
    db_session.registry.set(druks_db)
    note = await Note.create(body="An observation")

    @subscribe("note.delivery_failed", subject=Note)
    async def fail(**facts: object) -> None:
        raise RuntimeError("Subscriber unavailable")

    with pytest.raises(RuntimeError, match="Subscriber unavailable"):
        async with druks_db.begin_nested():
            note.gist = "A summary"
            await note.announce("note.delivery_failed")

    await druks_db.refresh(note)
    assert not note.gist
    assert not list(await druks_db.scalars(select(Event).filter_by(type="note.delivery_failed")))

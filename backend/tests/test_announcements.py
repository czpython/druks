import pytest
from druks.apps import loader
from druks.events.models import Event
from druks.signals import subscribe
from druks.workflows import Subject
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


async def test_stored_subject_keeps_distinct_domain_changes(druks_db):
    note = await Note.create(body="An observation")

    await note.announce("note.revised", revision=1)
    await note.announce("note.revised", revision=2)

    events = list(
        await druks_db.scalars(select(Event).filter_by(type="note.revised").order_by(Event.id))
    )
    assert [event.payload for event in events] == [{"revision": 1}, {"revision": 2}]
    assert {event.app for event in events} == {"field_notes"}
    assert {event.subject_id for event in events} == {str(note.id)}
    assert {event.subject_label for event in events} == {note.label}


async def test_domain_rollback_removes_the_change_and_announcement(druks_db):
    note = await Note.create(body="An observation")

    with pytest.raises(ValueError, match="Rejected change"):
        async with druks_db.begin_nested():
            note.gist = "A summary"
            await note.announce("note.summarized", gist=note.gist)
            raise ValueError("Rejected change")

    await druks_db.refresh(note)
    assert note.gist is None
    assert not list(await druks_db.scalars(select(Event).filter_by(type="note.summarized")))


async def test_subject_delivery_error_rolls_back_with_the_domain_transaction(druks_db):
    note = await Note.create(body="An observation")

    @subscribe("note.delivery_failed", subject=Note)
    async def fail(**facts: object) -> None:
        raise RuntimeError("Subscriber unavailable")

    with pytest.raises(RuntimeError, match="Subscriber unavailable"):
        async with druks_db.begin_nested():
            note.gist = "A summary"
            await note.announce("note.delivery_failed")

    await druks_db.refresh(note)
    assert note.gist is None
    assert not list(await druks_db.scalars(select(Event).filter_by(type="note.delivery_failed")))

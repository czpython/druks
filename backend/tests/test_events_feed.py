from unittest.mock import AsyncMock, Mock

from druks.accounts.models import Account
from druks.events.models import Event
from druks.events.routes import stream_feed
from druks.models import StoredSubject
from druks.teams.models import Team
from druks_field_notes.models import Note
from druks_field_notes.workflows import Summarize
from sqlalchemy import select


class Crate(StoredSubject):
    __tablename__ = "faketest_crates"

    def __str__(self) -> str:
        return f"CRATE-{self.id}"


class Pallet(StoredSubject):
    __tablename__ = "faketest_pallets"


async def test_feed_carries_what_a_row_is_worded_from(druks_db, druks_client):
    note = await Note.create(body="the pump ran hot")
    await Event.emit(
        druks_db,
        type="workflow.scheduled",
        subject=note.identity,
        key=note.key,
        app="field_notes",
        kind=Summarize.kind,
        run="wf1",
    )
    await Event.emit(
        druks_db, type="summarized", subject=note.identity, key=note.key, app="field_notes"
    )
    await druks_db.flush()

    items = (await druks_client.get("/api/events")).json()["items"]
    by_topic = {row["topic"]: row for row in items}

    started = by_topic["workflow.scheduled"]
    assert (started["app"], started["payload"]["kind"]) == ("field_notes", Summarize.kind)
    assert (started["subjectType"], started["subjectId"]) == ("note", str(note.id))
    # A note declares no label of its own, so it shows itself by identity.
    assert started["subjectKey"] == f"note {note.id}"

    # A milestone has no workflow behind it.
    assert "kind" not in by_topic["summarized"]["payload"]


async def test_every_subject_shows_itself(druks_db, druks_client):
    # A subject that declares a handle reads as it; one that doesn't reads by
    # identity. Either way it is snapshotted, so the row survives the row itself.
    crate, pallet = Crate(id=7), Pallet(id=7)
    druks_db.add_all([crate, pallet])
    await druks_db.flush()
    assert crate.identity == {"type": "crate", "id": 7}
    for subject in (crate, pallet):
        await Event.emit(
            druks_db,
            type="stocked",
            subject=subject.identity,
            key=subject.key,
            app="field_notes",
        )
    await druks_db.delete(crate)
    await druks_db.flush()

    items = (await druks_client.get("/api/events", params={"app": "field_notes"})).json()["items"]
    by_type = {row["subjectType"]: row for row in items}

    assert by_type["crate"]["subjectKey"] == "CRATE-7"
    assert by_type["pallet"]["subjectKey"] == "pallet 7"


async def test_feed_paginates_same_second_events_without_loss_or_repeat(druks_db, druks_client):
    # utc_now truncates to whole seconds, so these all share a created_at. Paging on
    # the truncated timestamp used to drop the whole second on the next page; paging on
    # the monotonic pk covers every event exactly once.
    for i in range(5):
        await Event.emit(druks_db, type=f"evt-{i}", app="field_notes")
    await druks_db.flush()

    collected = []
    params = {"limit": 2}
    for _ in range(10):  # bounded so a paging bug can't loop forever
        page = (await druks_client.get("/api/events", params=params)).json()
        collected.extend(page["items"])
        if not page["nextCursor"]:
            break
        params = {"limit": 2, "before": page["nextCursor"]}

    seqs = [item["seq"] for item in collected]
    assert len(seqs) == len(set(seqs)), "no event repeats across pages"
    assert {f"evt-{i}" for i in range(5)} <= {item["topic"] for item in collected}
    assert seqs == sorted(seqs, reverse=True), "strictly descending by seq"


async def test_a_member_outside_the_apps_team_sees_none_of_its_activity(druks_db, druks_client):
    await Account.get_or_create(druks_db, "admin@example.com")
    member = await Account.get_or_create(druks_db, "op@example.com")
    await Team.create(druks_db, name="Notes", member_ids=[], apps=["field_notes"])
    note = await Note.create(body="restricted")
    await Event.emit(
        druks_db, type="summarized", subject=note.identity, key=note.key, app="field_notes"
    )
    await Event.emit(druks_db, type="built", app="software_factory")
    await druks_db.flush()
    hidden = await druks_db.scalar(select(Event.id).where(Event.type == "summarized"))

    items = (await druks_client.get("/api/events")).json()["items"]
    topics = (await druks_client.get("/api/events/topics")).json()
    destinations = await druks_client.get(f"/api/events/{hidden}/destinations")
    request = Mock(headers={}, is_disconnected=AsyncMock(return_value=False))
    stream = await stream_feed(request, engine=druks_db.bind, account=member)
    streamed = await anext(stream.body_iterator)
    await stream.body_iterator.aclose()

    assert [item["topic"] for item in items] == ["built"]
    assert topics == [{"app": "software_factory", "topic": "built"}]
    assert destinations.status_code == 404
    assert '"topic":"built"' in streamed
    assert "summarized" not in streamed

from druks.events.models import Event
from druks.models import StoredSubject
from druks_field_notes.models import Note
from druks_field_notes.workflows import Summarize


class Crate(StoredSubject):
    __tablename__ = "faketest_crates"

    def get_label(self) -> str:
        return f"CRATE-{self.id}"


class Pallet(StoredSubject):
    __tablename__ = "faketest_pallets"


async def test_feed_carries_what_a_row_is_worded_from(druks_db, druks_client):
    note = await Note.create(body="the pump ran hot")
    await Event.emit(
        type="workflow.scheduled",
        subject=note.identity,
        label=note.label,
        app="field_notes",
        payload={"kind": Summarize.kind, "run": "wf1"},
    )
    await Event.emit(type="summarized", subject=note.identity, label=note.label, app="field_notes")
    await druks_db.flush()

    items = (await druks_client.get("/api/events")).json()["items"]
    by_topic = {row["topic"]: row for row in items}

    started = by_topic["workflow.scheduled"]
    assert (started["app"], started["workflow"]) == ("field_notes", Summarize.kind)
    assert (started["subjectType"], started["subjectId"]) == ("note", str(note.id))
    # A note declares no label of its own, so it shows itself by identity.
    assert started["subjectLabel"] == f"note {note.id}"

    # A milestone has no workflow behind it.
    assert by_topic["summarized"]["workflow"] is None


async def test_every_subject_shows_itself(druks_db, druks_client):
    # A subject that declares a handle reads as it; one that doesn't reads by
    # identity. Either way it is snapshotted, so the row survives the row itself.
    crate, pallet = Crate(id=7), Pallet(id=7)
    druks_db.add_all([crate, pallet])
    await druks_db.flush()
    assert crate.identity == {"type": "crate", "id": 7}
    for subject in (crate, pallet):
        await Event.emit(
            type="stocked", subject=subject.identity, label=subject.label, app="field_notes"
        )
    await druks_db.delete(crate)
    await druks_db.flush()

    items = (await druks_client.get("/api/events", params={"app": "field_notes"})).json()["items"]
    by_type = {row["subjectType"]: row for row in items}

    assert by_type["crate"]["subjectLabel"] == "CRATE-7"
    assert by_type["pallet"]["subjectLabel"] == "pallet 7"


async def test_feed_paginates_same_second_events_without_loss_or_repeat(druks_db, druks_client):
    # utc_now truncates to whole seconds, so these all share a created_at. Paging on
    # the truncated timestamp used to drop the whole second on the next page; paging on
    # the monotonic pk covers every event exactly once.
    for i in range(5):
        await Event.emit(type=f"evt-{i}", app="field_notes")
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

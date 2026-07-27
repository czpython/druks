from druks.events.builder import build_feed
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


def test_feed_carries_the_facts_a_row_is_worded_from(druks_db):
    note = Note.create(body="the pump ran hot")
    Event.emit(
        type="workflow.running",
        subject=note.identity,
        label=note.label,
        extension="field_notes",
        payload={"kind": Summarize.kind, "run": "wf1"},
    )
    Event.emit(
        type="summarized",
        subject=note.identity,
        label=note.label,
        extension="field_notes",
        payload={"words": 12},
    )
    druks_db.flush()

    by_kind = {row.kind: row for row in build_feed()[0]}

    started = by_kind["workflow.running"]
    assert (started.extension, started.workflow) == ("field_notes", Summarize.kind)
    assert (started.subject_type, started.subject_id) == ("note", str(note.id))
    # A note declares no label of its own, so it shows itself by identity; whatever
    # is left of the payload once the workflow is promoted out of it is its facts.
    assert (started.subject_label, started.facts) == (f"note {note.id}", {"run": "wf1"})

    # A milestone has no workflow behind it, and carries what its writer stated.
    summarized = by_kind["summarized"]
    assert (summarized.workflow, summarized.facts) == (None, {"words": 12})


def test_every_subject_shows_itself(druks_db):
    # A subject that declares a handle reads as it; one that doesn't reads by
    # identity. Either way it is snapshotted, so the row survives the row itself.
    crate, pallet = Crate(id=7), Pallet(id=7)
    druks_db.add_all([crate, pallet])
    druks_db.flush()
    assert crate.identity == {"type": "crate", "id": 7}
    for subject in (crate, pallet):
        Event.emit(
            type="stocked", subject=subject.identity, label=subject.label, extension="faketest"
        )
    druks_db.delete(crate)
    druks_db.flush()

    by_type = {row.subject_type: row for row in build_feed()[0] if row.extension == "faketest"}

    assert by_type["crate"].subject_label == "CRATE-7"
    assert by_type["pallet"].subject_label == "pallet 7"


def test_feed_paginates_same_second_events_without_loss_or_repeat(druks_db):
    # utc_now truncates to whole seconds, so these all share a created_at. Paging on
    # the truncated timestamp used to drop the whole second on the next page; paging on
    # the monotonic pk covers every event exactly once.
    for i in range(5):
        Event.emit(type=f"evt-{i}")
    druks_db.flush()

    collected = []
    cursor = None
    for _ in range(10):  # bounded so a paging bug can't loop forever
        page, cursor = build_feed(before=int(cursor) if cursor else None, limit=2)
        collected.extend(page)
        if cursor is None:
            break

    seqs = [item.seq for item in collected]
    assert len(seqs) == len(set(seqs)), "no event repeats across pages"
    assert {f"evt-{i}" for i in range(5)} <= {item.kind for item in collected}
    assert seqs == sorted(seqs, reverse=True), "strictly descending by seq"

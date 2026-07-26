from druks.events.builder import build_feed
from druks.events.models import Event
from druks_field_notes.models import Note
from druks_field_notes.workflows import Summarize


def test_feed_reads_run_and_milestone_events(druks_db):
    note = Note.create(body="the pump ran hot")
    Event.emit(
        type="workflow.running",
        subject=note.identity,
        extension="field_notes",
        payload={"kind": Summarize.kind, "run": "wf1"},
    )
    Event.emit(type="summarized", subject=note.identity, extension="field_notes")
    druks_db.flush()

    page, _ = build_feed()
    by_kind = {e.kind: e for e in page}
    assert "workflow.running" in by_kind and "summarized" in by_kind
    summarized = by_kind["summarized"]
    assert summarized.link_path == f"/app/field_notes/notes/{note.id}"
    assert f"note {note.id}" in summarized.summary
    assert f"note {note.id}" in by_kind["workflow.running"].summary


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

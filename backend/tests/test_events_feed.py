from conftest import make_test_work_item


def test_feed_reads_run_and_milestone_events(db_session):
    from druks.events.builder import build_feed
    from druks.events.models import Event

    item = make_test_work_item(repo="acme/extension", remote_key="ACME-9", title="t")
    Event.emit(
        type="run.running",
        subject=item.identity,
        extension="build",
        payload={"kind": "build.build_workflow", "run": "wf1"},
    )
    Event.emit(type="shipped", subject=item.identity, extension="build")
    db_session.flush()

    page, _ = build_feed()
    by_kind = {e.kind: e for e in page}
    assert "run.running" in by_kind and "milestone.shipped" in by_kind
    shipped = by_kind["milestone.shipped"]
    assert shipped.link_path == f"/work-items/{item.id}"
    assert "ACME-9" in shipped.summary
    assert "build_workflow started" in by_kind["run.running"].summary


def test_feed_paginates_same_second_events_without_loss_or_repeat(db_session):
    # utc_now truncates to whole seconds, so these all share a created_at. Paging on
    # the truncated timestamp used to drop the whole second on the next page; paging on
    # the monotonic pk covers every event exactly once.
    from druks.events.builder import build_feed
    from druks.events.models import Event

    for i in range(5):
        Event.emit(type=f"evt-{i}")
    db_session.flush()

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

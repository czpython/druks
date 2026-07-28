from datetime import UTC, datetime

from druks.contrib.ship.workflows import Build
from druks.testing import seed_run

from ship.factories import make_test_work_item


async def test_redispatch_to_a_new_run_clears_the_prior_attempts_pr(druks_db, monkeypatch) -> None:
    """A genuinely new run is a fresh attempt: dispatch points the item at it and
    drops the prior attempt's branch, PR and verdict — so a late close for the old
    PR can't resolve this item onto the new run, and a closed item comes back onto
    the board instead of resting in History with a live run."""
    seed_run(druks_db, kind=Build.kind, run_id="run-old")
    seed_run(druks_db, kind=Build.kind, run_id="run-new")
    item = make_test_work_item(repo="o/r", title="t", ticket_key="ACME-2")
    item.update(build_run_id="run-old", pr_number=7, branch="agent/old")
    item.resolve(merged=False, at=datetime.now(UTC))

    async def fake_start(cls, **kwargs):
        return "run-new"

    monkeypatch.setattr(Build, "start", classmethod(fake_start))
    await Build.dispatch(
        ticket={
            "source": item.source,
            "identifier": item.ticket_key,
            "status": "Ready",
            "title": item.title,
            "url": "https://tracker.test/ACME-2",
            "project_name": "r",
            "labels": [],
            "assignee_email": None,
            "assignee_name": None,
        }
    )

    assert item.build_run_id == "run-new"
    assert item.pr_number is None
    assert item.branch is None
    assert item.resolution is None
    assert item.resolved_at is None


async def test_duplicate_dispatch_keeps_the_live_attempt_routing(druks_db, monkeypatch) -> None:
    """A duplicate dispatch dedups to the live run — start() hands back its id —
    so the item's branch/PR must survive, or PR routing and board links break."""
    seed_run(druks_db, kind=Build.kind, run_id="run-live")
    item = make_test_work_item(repo="o/r", title="t", ticket_key="ACME-3")
    item.update(build_run_id="run-live", pr_number=7, branch="agent/live")

    async def dedup_start(cls, **kwargs):
        return "run-live"

    monkeypatch.setattr(Build, "start", classmethod(dedup_start))
    await Build.dispatch(
        ticket={
            "source": item.source,
            "identifier": item.ticket_key,
            "status": "Ready",
            "title": item.title,
            "url": "https://tracker.test/ACME-3",
            "project_name": "r",
            "labels": [],
            "assignee_email": None,
            "assignee_name": None,
        }
    )

    assert item.build_run_id == "run-live"
    assert item.pr_number == 7
    assert item.branch == "agent/live"


def test_update_clears_nullable_with_none_and_skips_omitted(druks_db) -> None:
    """update() tells a clear from a skip: pr_number=None clears the column,
    while leaving branch out preserves it."""
    item = make_test_work_item(repo="o/r", title="t", ticket_key="ACME-4")
    item.update(pr_number=9, branch="agent/keep")

    item.update(pr_number=None)

    assert item.pr_number is None
    assert item.branch == "agent/keep"

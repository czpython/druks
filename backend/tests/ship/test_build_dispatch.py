from druks.contrib.ship.workflows import Build
from druks.models import Base
from druks.testing import seed_run

from ship.factories import make_test_work_item


async def test_dispatch_does_not_clear_a_stored_pr_resolution(druks_db, monkeypatch) -> None:
    item = make_test_work_item(repo="o/r", title="t", ticket_key="ACME-1")
    item.pr_merged = False
    item.pr_resolved_at = Base.utc_now()
    seed_run(druks_db, kind=Build.kind, run_id="run-1")
    started = {}

    async def fake_start(cls, **kwargs):
        started.update(kwargs)
        return "run-1"

    monkeypatch.setattr(Build, "start", classmethod(fake_start))
    run_id = await Build.dispatch(
        ticket={
            "source": item.source,
            "identifier": item.ticket_key,
            "status": "Ready",
            "title": item.title,
            "url": "https://tracker.test/ACME-1",
            "project_name": "r",
            "labels": [],
            "assignee_email": None,
            "assignee_name": None,
        }
    )

    assert run_id == "run-1"
    assert item.build_run_id == "run-1"
    assert item.pr_merged is False
    assert item.pr_resolved_at is not None
    assert started["ticket_ref"] == "ACME-1"
    assert started["ticket_title"] == "t"
    assert started["ticket_url"] == "https://tracker.test/ACME-1"


async def test_redispatch_to_a_new_run_clears_prior_attempt_branch_and_pr(
    druks_db, monkeypatch
) -> None:
    """A genuinely new run is a fresh attempt: dispatch points the item at it and
    drops the prior attempt's branch/PR, so a late close for the old PR can't
    resolve this item onto the new run."""
    seed_run(druks_db, kind=Build.kind, run_id="run-old")
    seed_run(druks_db, kind=Build.kind, run_id="run-new")
    item = make_test_work_item(repo="o/r", title="t", ticket_key="ACME-2")
    item.update(build_run_id="run-old", pr_number=7, branch="agent/old")

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

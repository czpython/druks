from datetime import UTC, datetime

from druks.contrib.ship.workflows import Build
from druks.testing import seed_run

from ship.factories import make_test_work_item


async def test_dispatch_leaves_the_item_alone(druks_db, monkeypatch) -> None:
    """Dispatch starts the build and touches nothing else — clearing the previous
    attempt is the scheduled reaction's (test_lane_reactions), and a duplicate
    dispatch never makes that announcement."""
    seed_run(druks_db, kind=Build.kind, run_id="run-old")
    seed_run(druks_db, kind=Build.kind, run_id="run-new")
    item = make_test_work_item(repo="o/r", title="t", ticket_key="ACME-2")
    item.update(pr_number=7, branch="agent/old")
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

    assert item.pr_number == 7
    assert item.branch == "agent/old"
    assert item.resolution == "closed"


def test_update_clears_nullable_with_none_and_skips_omitted(druks_db) -> None:
    """update() tells a clear from a skip: pr_number=None clears the column,
    while leaving branch out preserves it."""
    item = make_test_work_item(repo="o/r", title="t", ticket_key="ACME-4")
    item.update(pr_number=9, branch="agent/keep")

    item.update(pr_number=None)

    assert item.pr_number is None
    assert item.branch == "agent/keep"

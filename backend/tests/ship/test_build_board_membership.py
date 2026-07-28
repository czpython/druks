from datetime import UTC, datetime, timedelta

import druks.contrib.ship.workflows  # noqa: F401  # registers ship.build, the seeded kind
import pytest
from druks.contrib.ship.models import WorkItem

from ship.factories import make_test_work_item, seed_build_run


def _board_ids(druks_db):
    druks_db.expire_all()
    return {row.id for row in WorkItem.list_summaries()}


def _resolve(item, *, merged=True, at=None):
    item.resolve(merged=merged, at=at or datetime.now(UTC))


@pytest.mark.parametrize(
    "state", ["scheduled", "running", "parked", "failed", "finished", "cancelled"]
)
def test_an_unresolved_item_holds_the_board(druks_db, state):
    # Until GitHub answers, the work is still druks's — whatever its run is doing.
    # Where the run stands colours the row; it never decides whether the row is here.
    item = make_test_work_item(repo="ClawHaven/acme-app", title=f"build {state}")
    seed_build_run(druks_db, work_item_id=item.id, state=state)
    assert str(item.id) in _board_ids(druks_db)


@pytest.mark.parametrize("state", ["running", "failed", "finished"])
def test_a_resolved_pr_leaves_the_board_whatever_its_run_says(druks_db, state):
    # The strand this fixes: membership read druks's own opinion, which a manual
    # merge after a failed run never changed, so the item lingered forever.
    item = make_test_work_item(repo="ClawHaven/acme-app", title=f"resolved {state}")
    seed_build_run(druks_db, work_item_id=item.id, state=state)
    _resolve(item)
    assert str(item.id) not in _board_ids(druks_db)


def test_a_redispatched_item_returns_to_the_board(druks_db):
    # Its newest run drives it, and starting a build drops the previous PR's
    # verdict along with the branch and number it belonged to.
    item = make_test_work_item(repo="ClawHaven/acme-app", title="rebuilt")
    seed_build_run(druks_db, work_item_id=item.id, state="finished")
    _resolve(item, merged=False)
    run = seed_build_run(druks_db, work_item_id=item.id, state="running")
    item.start_attempt(run.id)

    assert item.resolution is None
    assert str(item.id) in _board_ids(druks_db)


def test_the_board_does_not_ask_about_runs(druks_db):
    # Dispatch creates the item and starts its run in one transaction, so a
    # run-less item is not a production shape — and the board no longer asks.
    item = make_test_work_item(repo="ClawHaven/acme-app", title="never dispatched")
    assert str(item.id) in _board_ids(druks_db)


def test_history_holds_the_resolved_newest_verdict_first(druks_db):
    older = make_test_work_item(repo="ClawHaven/acme-app", title="closed first")
    _resolve(older, merged=False, at=datetime.now(UTC) - timedelta(minutes=1))
    newer = make_test_work_item(repo="ClawHaven/acme-app", title="merged after")
    _resolve(newer)
    make_test_work_item(repo="ClawHaven/acme-app", title="still open")

    assert [item.id for item in WorkItem.list_handoff()] == [newer.id, older.id]


def test_the_newest_run_speaks_for_the_item(druks_db):
    # One rule, two implementations — the bulk board query and the per-subject
    # status must name the same driving run or the board and its lanes disagree.
    item = make_test_work_item(repo="ClawHaven/acme-app", title="two runs")
    seed_build_run(druks_db, work_item_id=item.id, state="cancelled")
    seed_build_run(druks_db, work_item_id=item.id, state="parked", input_gate="review")
    druks_db.expire_all()
    assert item.get_status().state == "parked"
    assert str(item.id) in _board_ids(druks_db)

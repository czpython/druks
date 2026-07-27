import druks.contrib.ship.workflows  # noqa: F401  # registers ship.build, the seeded kind
import pytest
from druks.contrib.ship.enums import HandoffStatus
from druks.contrib.ship.extension import Ship
from druks.contrib.ship.models import WorkItem

from ship.factories import make_test_work_item, seed_build_run


def _board_ids(druks_db):
    druks_db.expire_all()
    return {row.id for row in Ship.list_subjects()}


@pytest.mark.parametrize("state", ["scheduled", "running", "parked", "failed"])
def test_a_live_or_failed_build_holds_the_board(druks_db, state):
    # A failed run still wants the operator, so it stays alongside the live ones.
    item = make_test_work_item(repo="ClawHaven/acme-app", title=f"build {state}")
    seed_build_run(druks_db, work_item_id=item.id, state=state)
    assert str(item.id) in _board_ids(druks_db)


@pytest.mark.parametrize("state", ["finished", "cancelled"])
def test_an_ended_build_leaves_the_board(druks_db, state):
    # The strand this fixes: membership used to read work_items.status, which a
    # cancelled or errored run never wrote, so the item lingered forever.
    item = make_test_work_item(repo="ClawHaven/acme-app", title=f"build {state}")
    seed_build_run(druks_db, work_item_id=item.id, state=state)
    druks_db.expire_all()
    assert item.status is None
    assert str(item.id) not in _board_ids(druks_db)


def test_a_redispatched_item_returns_to_the_board(druks_db):
    # Its newest run drives it, so a fresh build outranks the handed-off one.
    item = make_test_work_item(repo="ClawHaven/acme-app", title="rebuilt")
    seed_build_run(druks_db, work_item_id=item.id, state="finished")
    seed_build_run(druks_db, work_item_id=item.id, state="running")
    assert str(item.id) in _board_ids(druks_db)


def test_an_item_without_runs_stays_off_the_board(druks_db):
    item = make_test_work_item(repo="ClawHaven/acme-app", title="never dispatched")
    assert str(item.id) not in _board_ids(druks_db)


async def test_a_cancelled_build_settles_as_cancelled(druks_db):
    # Nothing merged, so History records the abandonment.
    from druks.contrib.ship.subscribers import build_end_settles_the_item

    item = make_test_work_item(repo="ClawHaven/acme-app", title="cancelled build")
    await build_end_settles_the_item(subject=item)
    assert item.status == HandoffStatus.CANCELLED


async def test_a_shipped_item_keeps_its_outcome(druks_db):
    # ship() lands first and owns the verdict; the run's own cancel must not
    # overwrite it on the way out.
    from druks.contrib.ship.subscribers import build_end_settles_the_item

    item = make_test_work_item(repo="ClawHaven/acme-app", title="merged build")
    item.set_status(HandoffStatus.SHIPPED)
    await build_end_settles_the_item(subject=item)
    assert item.status == HandoffStatus.SHIPPED


def test_history_holds_the_handed_off(druks_db):
    shipped = make_test_work_item(repo="ClawHaven/acme-app", title="shipped")
    shipped.set_status(HandoffStatus.SHIPPED)
    make_test_work_item(repo="ClawHaven/acme-app", title="still open")
    assert [item.id for item in WorkItem.list_handoff()] == [shipped.id]


def test_the_newest_run_speaks_for_the_item(druks_db):
    # One rule, two implementations — the bulk board query and the per-subject
    # status must name the same driving run or the board and its lanes disagree.
    item = make_test_work_item(repo="ClawHaven/acme-app", title="two runs")
    seed_build_run(druks_db, work_item_id=item.id, state="cancelled")
    seed_build_run(druks_db, work_item_id=item.id, state="parked", input_gate="review")
    druks_db.expire_all()
    assert item.get_status().state == "parked"
    assert str(item.id) in _board_ids(druks_db)

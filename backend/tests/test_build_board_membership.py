import druks.build.workflows  # noqa: F401  # registers build.build_workflow, the seeded kind
import pytest
from conftest import make_test_work_item, seed_build_run
from druks.build.enums import HandoffStatus
from druks.build.extension import Build
from druks.build.models import WorkItem


def _board_ids(db_session):
    db_session.expire_all()
    return {row.id for row in Build.list_subjects()}


@pytest.mark.parametrize("state", ["scheduled", "running", "pending_input", "failed"])
def test_a_live_or_failed_build_holds_the_board(db_session, state):
    # A failed run still wants the operator, so it stays alongside the live ones.
    item = make_test_work_item(repo="ClawHaven/acme-app", title=f"build {state}")
    seed_build_run(db_session, work_item_id=item.id, state=state)
    assert str(item.id) in _board_ids(db_session)


@pytest.mark.parametrize("state", ["finished", "cancelled"])
def test_an_ended_build_leaves_the_board(db_session, state):
    # The strand this fixes: membership used to read work_items.status, which a
    # cancelled or errored run never wrote, so the item lingered forever.
    item = make_test_work_item(repo="ClawHaven/acme-app", title=f"build {state}")
    seed_build_run(db_session, work_item_id=item.id, state=state)
    db_session.expire_all()
    assert item.status is None
    assert str(item.id) not in _board_ids(db_session)


def test_a_redispatched_item_returns_to_the_board(db_session):
    # Its newest run drives it, so a fresh build outranks the handed-off one.
    item = make_test_work_item(repo="ClawHaven/acme-app", title="rebuilt")
    seed_build_run(db_session, work_item_id=item.id, state="finished")
    seed_build_run(db_session, work_item_id=item.id, state="running")
    assert str(item.id) in _board_ids(db_session)


def test_an_item_without_runs_stays_off_the_board(db_session):
    item = make_test_work_item(repo="ClawHaven/acme-app", title="never dispatched")
    assert str(item.id) not in _board_ids(db_session)


async def test_a_cancelled_build_settles_as_cancelled(db_session):
    # Nothing merged, so History records the abandonment.
    from druks.build.subscribers import build_end_settles_the_item

    item = make_test_work_item(repo="ClawHaven/acme-app", title="cancelled build")
    await build_end_settles_the_item(subject=item)
    assert item.status == HandoffStatus.CANCELLED


async def test_a_shipped_item_keeps_its_outcome(db_session):
    # ship() lands first and owns the verdict; the run's own cancel must not
    # overwrite it on the way out.
    from druks.build.subscribers import build_end_settles_the_item

    item = make_test_work_item(repo="ClawHaven/acme-app", title="merged build")
    item.set_status(HandoffStatus.SHIPPED)
    await build_end_settles_the_item(subject=item)
    assert item.status == HandoffStatus.SHIPPED


def test_history_holds_the_handed_off(db_session):
    shipped = make_test_work_item(repo="ClawHaven/acme-app", title="shipped")
    shipped.set_status(HandoffStatus.SHIPPED)
    make_test_work_item(repo="ClawHaven/acme-app", title="still open")
    assert [item.id for item in WorkItem.list_handoff()] == [shipped.id]


def test_the_newest_run_speaks_for_the_item(db_session):
    # One rule, two implementations — the bulk board query and the per-subject
    # status must name the same driving run or the board and its lanes disagree.
    from druks.workflows import get_subject_status

    item = make_test_work_item(repo="ClawHaven/acme-app", title="two runs")
    seed_build_run(db_session, work_item_id=item.id, state="cancelled")
    seed_build_run(db_session, work_item_id=item.id, state="pending_input", input_gate="review")
    db_session.expire_all()
    assert get_subject_status(item.subject_type, str(item.id)).state == "pending_input"
    assert str(item.id) in _board_ids(db_session)

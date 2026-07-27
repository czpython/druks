from datetime import UTC, datetime, timedelta

import druks.contrib.ship.workflows  # noqa: F401  # registers ship.build, the seeded kind
import pytest
from druks.contrib.ship.extension import Ship
from druks.contrib.ship.models import WorkItem
from druks.durable.dbos_state import workflow_status
from druks.durable.enums import RunState
from druks.durable.models import Run

from ship.factories import make_test_work_item, seed_build_run


def _board_ids(druks_db):
    druks_db.expire_all()
    return {row.id for row in Ship.list_subjects()}


@pytest.mark.parametrize("state", ["scheduled", "running", "parked", "failed", "finished"])
def test_an_unresolved_build_holds_the_board(druks_db, state):
    item = make_test_work_item(repo="ClawHaven/acme-app", title=f"build {state}")
    seed_build_run(druks_db, work_item_id=item.id, state=state)

    assert str(item.id) in _board_ids(druks_db)


@pytest.mark.parametrize("state", ["running", "failed", "finished"])
def test_a_resolved_pr_leaves_the_board_before_run_state_is_considered(druks_db, state):
    item = make_test_work_item(repo="ClawHaven/acme-app", title=f"resolved {state}")
    seed_build_run(druks_db, work_item_id=item.id, state=state)
    item.pr_merged = True
    item.pr_resolved_at = datetime.now(UTC)
    druks_db.flush()

    assert str(item.id) not in _board_ids(druks_db)


def test_an_item_without_runs_stays_off_the_board(druks_db):
    item = make_test_work_item(repo="ClawHaven/acme-app", title="never dispatched")

    assert str(item.id) not in _board_ids(druks_db)


@pytest.mark.parametrize("state", ["cancelled", "orphaned"])
def test_a_cancelled_or_orphaned_build_with_no_resolution_leaves_the_board(
    druks_db, state
):
    item = make_test_work_item(repo="ClawHaven/acme-app", title=f"{state} build")
    if state == "cancelled":
        seed_build_run(druks_db, work_item_id=item.id, state=state)
    else:
        run = seed_build_run(druks_db, work_item_id=item.id)
        druks_db.execute(
            workflow_status.delete().where(workflow_status.c.workflow_uuid == run.id)
        )
        run.created_at = datetime.now(UTC) - timedelta(minutes=10)
        druks_db.flush()
        druks_db.expire(run, ["state"])
        assert Run.get(run.id).state == RunState.ORPHANED

    assert str(item.id) not in _board_ids(druks_db)


def test_history_holds_resolved_items_in_resolution_order(druks_db):
    older = make_test_work_item(repo="ClawHaven/acme-app", title="older")
    older.pr_merged = False
    older.pr_resolved_at = datetime.now(UTC) - timedelta(minutes=1)
    newer = make_test_work_item(repo="ClawHaven/acme-app", title="newer")
    newer.pr_merged = True
    newer.pr_resolved_at = datetime.now(UTC)
    make_test_work_item(repo="ClawHaven/acme-app", title="still open")
    druks_db.flush()

    assert [item.id for item in WorkItem.list_handoff()] == [newer.id, older.id]


def test_the_newest_run_state_speaks_for_the_item(druks_db):
    item = make_test_work_item(repo="ClawHaven/acme-app", title="two runs")
    seed_build_run(druks_db, work_item_id=item.id, state="finished")
    seed_build_run(druks_db, work_item_id=item.id, state="parked", input_gate="review")
    druks_db.expire_all()

    states = Run.subject_states(WorkItem.subject_type)

    assert states[str(item.id)] == RunState.PARKED
    assert item.get_status().state == RunState.PARKED
    assert str(item.id) in _board_ids(druks_db)

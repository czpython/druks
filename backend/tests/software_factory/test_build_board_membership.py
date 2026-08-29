from datetime import UTC, datetime, timedelta

import druks.contrib.software_factory.workflows  # noqa: F401  # registers software_factory.build, the seeded kind
import pytest
from druks.contrib.software_factory.models import WorkItem

from software_factory.factories import make_test_work_item, seed_build_run


async def _board_ids(druks_db):
    druks_db.expunge_all()
    return {row.id for row in await WorkItem.list_summaries(None)}


async def _resolve(item, *, merged=True, at=None):
    await item.resolve(merged=merged, at=at or datetime.now(UTC))


@pytest.mark.parametrize(
    "state", ["scheduled", "running", "parked", "failed", "finished", "cancelled"]
)
async def test_an_unresolved_item_holds_the_board(druks_db, state):
    item = await make_test_work_item(repo="ClawHaven/acme-app", title=f"build {state}")
    await seed_build_run(druks_db, work_item_id=item.id, state=state)
    assert str(item.id) in await _board_ids(druks_db)


@pytest.mark.parametrize("state", ["running", "failed", "finished"])
async def test_a_resolved_pr_leaves_the_board_whatever_its_run_says(druks_db, state):
    item = await make_test_work_item(repo="ClawHaven/acme-app", title=f"resolved {state}")
    await seed_build_run(druks_db, work_item_id=item.id, state=state)
    await _resolve(item)
    assert str(item.id) not in await _board_ids(druks_db)


async def test_a_redispatched_item_returns_to_the_board(druks_db):
    item = await make_test_work_item(repo="ClawHaven/acme-app", title="rebuilt")
    await _resolve(item, merged=False)

    await item.start_attempt()

    assert str(item.id) in await _board_ids(druks_db)


async def test_the_board_does_not_ask_about_runs(druks_db):
    item = await make_test_work_item(repo="ClawHaven/acme-app", title="never dispatched")
    assert str(item.id) in await _board_ids(druks_db)


async def test_history_holds_the_resolved_newest_verdict_first(druks_db):
    older = await make_test_work_item(repo="ClawHaven/acme-app", title="closed first")
    await _resolve(older, merged=False, at=datetime.now(UTC) - timedelta(minutes=1))
    newer = await make_test_work_item(repo="ClawHaven/acme-app", title="merged after")
    await _resolve(newer)
    await make_test_work_item(repo="ClawHaven/acme-app", title="still open")

    assert [item.id for item in await WorkItem.list_handoff()] == [newer.id, older.id]


async def test_the_newest_run_speaks_for_the_item(druks_db):
    # One rule, two implementations — the bulk board query and the per-subject
    # status must name the same driving run or the board and its lanes disagree.
    item = await make_test_work_item(repo="ClawHaven/acme-app", title="two runs")
    await seed_build_run(druks_db, work_item_id=item.id, state="cancelled")
    await seed_build_run(druks_db, work_item_id=item.id, state="parked", input_gate="review")
    druks_db.expunge_all()
    assert (await item.get_status()).state == "parked"
    assert str(item.id) in await _board_ids(druks_db)

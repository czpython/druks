from conftest import make_test_work_item, seed_run
from druks.build.enums import HandoffStatus
from druks.build.workflows import BuildWorkflow


async def test_dispatch_pulls_scoped_item_back_onto_the_board(db_session, monkeypatch) -> None:
    """A scoped item rests in History; dispatching its build must clear the
    handoff status so the active board shows the run (and its gates) instead
    of a stale "Scoped" row."""
    item = make_test_work_item(repo="o/r", title="t", remote_key="ACME-1")
    item.set_status(HandoffStatus.SCOPED)
    seed_run(db_session, "run-1")

    async def fake_start(cls, **kwargs):
        return "run-1"

    monkeypatch.setattr(BuildWorkflow, "start", classmethod(fake_start))
    run_id = await BuildWorkflow.dispatch(work_item_id=item.id)

    assert run_id == "run-1"
    assert item.build_run_id == "run-1"
    assert item.status is None


async def test_redispatch_to_a_new_run_clears_prior_attempt_branch_and_pr(
    db_session, monkeypatch
) -> None:
    """A genuinely new run is a fresh attempt: dispatch points the item at it and
    drops the prior attempt's branch/PR, so a late close for the old PR can't
    resolve this item onto the new run."""
    seed_run(db_session, "run-old")
    seed_run(db_session, "run-new")
    item = make_test_work_item(repo="o/r", title="t", remote_key="ACME-2")
    item.update(build_run_id="run-old", pr_number=7, branch="agent/old")

    async def fake_start(cls, **kwargs):
        return "run-new"

    monkeypatch.setattr(BuildWorkflow, "start", classmethod(fake_start))
    await BuildWorkflow.dispatch(work_item_id=item.id)

    assert item.build_run_id == "run-new"
    assert item.pr_number is None
    assert item.branch is None


async def test_duplicate_dispatch_keeps_the_live_attempt_routing(db_session, monkeypatch) -> None:
    """A duplicate dispatch dedups to the live run — start() hands back its id —
    so the item's branch/PR must survive, or PR routing and board links break."""
    seed_run(db_session, "run-live")
    item = make_test_work_item(repo="o/r", title="t", remote_key="ACME-3")
    item.update(build_run_id="run-live", pr_number=7, branch="agent/live")

    async def dedup_start(cls, **kwargs):
        return "run-live"

    monkeypatch.setattr(BuildWorkflow, "start", classmethod(dedup_start))
    await BuildWorkflow.dispatch(work_item_id=item.id)

    assert item.build_run_id == "run-live"
    assert item.pr_number == 7
    assert item.branch == "agent/live"


def test_update_clears_nullable_with_none_and_skips_omitted(db_session) -> None:
    """update() tells a clear from a skip: pr_number=None clears the column,
    while leaving branch out preserves it."""
    item = make_test_work_item(repo="o/r", title="t", remote_key="ACME-4")
    item.update(pr_number=9, branch="agent/keep")

    item.update(pr_number=None)

    assert item.pr_number is None
    assert item.branch == "agent/keep"

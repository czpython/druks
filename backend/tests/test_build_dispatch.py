from conftest import make_test_work_item, seed_run
from druks.build.enums import HandoffStatus
from druks.build.workflows import BuildWorkflow
from druks.workflows import WorkflowStartResult


async def test_dispatch_pulls_scoped_item_back_onto_the_board(db_session, monkeypatch) -> None:
    """A scoped item rests in History; dispatching its build must clear the
    handoff status so the active board shows the run (and its gates) instead
    of a stale "Scoped" row."""
    item = make_test_work_item(repo="o/r", title="t", remote_key="ACME-1")
    item.set_status(HandoffStatus.SCOPED)
    seed_run(db_session, "run-1")

    async def fake_start(cls, **kwargs):
        return WorkflowStartResult(run_id="run-1", is_duplicate=False)

    monkeypatch.setattr(BuildWorkflow, "start", classmethod(fake_start))
    run_id = await BuildWorkflow.dispatch(work_item_id=item.id)

    assert run_id == "run-1"
    assert item.build_run_id == "run-1"
    assert item.status is None

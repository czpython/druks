from datetime import timedelta

import pytest
from conftest import seed_dbos_status
from druks.build.contracts import ReviewWork
from druks.build.workflows import BuildWorkflow, Scope, ScopeReply
from druks.durable.models import Run
from druks.durable.reads import get_subject_phase
from druks.models import Base
from druks.workflows import WorkflowError
from uuid_utils import uuid7

pytestmark = pytest.mark.asyncio


def _subject_run(
    db_session,
    *,
    subject: dict,
    kind: str,
    state: str,
    order: int = 0,
    gate: str | None = None,
) -> Run:
    run = Run(
        id=str(uuid7()),
        kind=kind,
        input_gate=gate,
        created_at=Base.utc_now() + timedelta(seconds=order),
    )
    db_session.add(run)
    db_session.flush()
    seed_dbos_status(db_session, run.id, state, subject=subject)
    return run


async def test_gate_answer_resumes_only_a_run_parked_on_its_gate(db_session, monkeypatch):
    # A subject can carry runs of several workflows at once; the gate names which one
    # answers, so a newer run of another kind never hides the parked one. A timed-out
    # run keeps its stale ``input_gate``, so parked-ness decides, not that column.
    subject = {"type": "work_item", "id": 1}
    parked = _subject_run(
        db_session, subject=subject, kind=Scope.kind, state="pending_input", gate=ScopeReply.name
    )
    _subject_run(db_session, subject=subject, kind=BuildWorkflow.kind, state="running", order=1)
    resumed = []

    async def resume(self, **reply):
        resumed.append(self.id)

    monkeypatch.setattr(Run, "resume", resume)

    await ScopeReply.answer(subject)
    assert resumed == [parked.id]

    timed_out = {"type": "work_item", "id": 2}
    _subject_run(
        db_session,
        subject=timed_out,
        kind=BuildWorkflow.kind,
        state="failed",
        gate=ReviewWork.name,
    )
    with pytest.raises(WorkflowError, match="is not parked"):
        await ReviewWork.answer(timed_out, action="approve")
    assert resumed == [parked.id]


async def test_workflow_cancel_takes_its_own_kind_and_passes_over_idle_subjects(
    db_session, monkeypatch
):
    # Webhooks redeliver, and a PR can close long after its build ended: cancelling what
    # is already gone is the no-op the caller expects, not an error.
    subject = {"type": "work_item", "id": 3}
    build = _subject_run(db_session, subject=subject, kind=BuildWorkflow.kind, state="running")
    _subject_run(db_session, subject=subject, kind=Scope.kind, state="running", order=1)
    cancelled = []

    async def cancel(self, *, failure=None):
        cancelled.append(self.id)

    monkeypatch.setattr(Run, "cancel", cancel)

    await BuildWorkflow.cancel(subject)
    assert cancelled == [build.id]

    idle = {"type": "work_item", "id": 4}
    _subject_run(db_session, subject=idle, kind=BuildWorkflow.kind, state="finished")
    await BuildWorkflow.cancel(idle)
    assert cancelled == [build.id]


async def test_subject_phase_reads_the_driving_running_workflow(db_session, monkeypatch):
    subject = {"type": "work_item", "id": 5}
    _subject_run(
        db_session, subject=subject, kind=Scope.kind, state="pending_input", gate=ScopeReply.name
    )
    driving = _subject_run(
        db_session, subject=subject, kind=BuildWorkflow.kind, state="running", order=1
    )
    seen = []

    async def phase(workflow_id):
        seen.append(workflow_id)
        return "agent_running"

    monkeypatch.setattr("druks.durable.reads.get_run_phase", phase)

    assert await get_subject_phase(subject["type"], str(subject["id"])) == "agent_running"
    assert seen == [driving.id]

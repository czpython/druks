from datetime import timedelta

import pytest
from conftest import make_test_work_item, seed_dbos_status
from druks.contrib.ship.contracts import ReviewWork
from druks.contrib.ship.models import WorkItem
from druks.contrib.ship.workflows import Build, Profile
from druks.durable.models import Run
from druks.durable.reads import get_subject_phase
from druks.models import Base
from druks.workflows import OperatorReply, WorkflowError
from uuid_utils import uuid7

pytestmark = pytest.mark.asyncio


def _work_item(**fields):
    return make_test_work_item(repo="ClawHaven/acme-app", title="probe", **fields)


def _subject_run(
    db_session,
    *,
    subject: WorkItem,
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
    seed_dbos_status(db_session, run.id, state, subject=subject.identity)
    return run


async def test_gate_answer_resumes_only_a_run_parked_on_its_gate(db_session, monkeypatch):
    # A subject can carry runs of several workflows at once; the gate names which one
    # answers, so a newer run of another kind never hides the parked one. A timed-out
    # run keeps its stale ``input_gate``, so parked-ness decides, not that column.
    subject = _work_item(remote_key="ENG-748-A")
    parked = _subject_run(
        db_session,
        subject=subject,
        kind=Build.kind,
        state="pending_input",
        gate=OperatorReply.name,
    )
    _subject_run(db_session, subject=subject, kind=Profile.kind, state="running", order=1)
    resumed = []

    async def resume(self, **reply):
        resumed.append(self.id)

    monkeypatch.setattr(Run, "resume", resume)

    await OperatorReply.answer(subject, action="approve")
    assert resumed == [parked.id]

    timed_out = _work_item(remote_key="ENG-748-B")
    _subject_run(
        db_session,
        subject=timed_out,
        kind=Build.kind,
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
    subject = _work_item(remote_key="ENG-748-C")
    build = _subject_run(db_session, subject=subject, kind=Build.kind, state="running")
    _subject_run(db_session, subject=subject, kind=Profile.kind, state="running", order=1)
    cancelled = []

    async def cancel(self, *, failure=None):
        cancelled.append(self.id)

    monkeypatch.setattr(Run, "cancel", cancel)

    await Build.cancel(subject)
    assert cancelled == [build.id]

    idle = _work_item(remote_key="ENG-748-D")
    _subject_run(db_session, subject=idle, kind=Build.kind, state="finished")
    await Build.cancel(idle)
    assert cancelled == [build.id]


async def test_subject_phase_reads_the_driving_running_workflow(db_session, monkeypatch):
    subject = _work_item(remote_key="ENG-748-E")
    _subject_run(
        db_session,
        subject=subject,
        kind=Build.kind,
        state="pending_input",
        gate=OperatorReply.name,
    )
    driving = _subject_run(db_session, subject=subject, kind=Profile.kind, state="running", order=1)
    seen = []

    async def phase(workflow_id):
        seen.append(workflow_id)
        return "agent_running"

    monkeypatch.setattr("druks.durable.reads.get_run_phase", phase)

    assert await get_subject_phase(subject.subject_type, str(subject.id)) == "agent_running"
    assert seen == [driving.id]

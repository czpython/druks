from datetime import timedelta

import pytest
from druks.contrib.ship.contracts import ReviewWork
from druks.contrib.ship.models import WorkItem
from druks.contrib.ship.workflows import Build, Profile
from druks.durable.models import Run
from druks.models import Base
from druks.testing import seed_dbos_status
from druks.workflows import OperatorReply, WorkflowError
from uuid_utils import uuid7

from ship.factories import make_test_work_item

pytestmark = pytest.mark.asyncio


def _work_item(**fields):
    return make_test_work_item(repo="ClawHaven/acme-app", title="probe", **fields)


def _subject_run(
    druks_db,
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
    druks_db.add(run)
    druks_db.flush()
    seed_dbos_status(druks_db, run.id, state, subject=subject.identity)
    return run


async def test_gate_answer_resumes_only_a_run_parked_on_its_gate(druks_db, monkeypatch):
    # A subject can carry runs of several workflows at once; the gate names which one
    # answers, so a newer run of another kind never hides the parked one. A timed-out
    # run keeps its stale ``input_gate``, so parked-ness decides, not that column.
    subject = _work_item(remote_key="ENG-748-A")
    parked = _subject_run(
        druks_db,
        subject=subject,
        kind=Build.kind,
        state="parked",
        gate=OperatorReply.name,
    )
    _subject_run(druks_db, subject=subject, kind=Profile.kind, state="running", order=1)
    resumed = []

    async def resume(self, **reply):
        resumed.append(self.id)

    monkeypatch.setattr(Run, "resume", resume)

    await OperatorReply.answer(subject, action="approve")
    assert resumed == [parked.id]

    timed_out = _work_item(remote_key="ENG-748-B")
    _subject_run(
        druks_db,
        subject=timed_out,
        kind=Build.kind,
        state="failed",
        gate=ReviewWork.name,
    )
    with pytest.raises(WorkflowError, match="is not parked"):
        await ReviewWork.answer(timed_out, action="approve")
    assert resumed == [parked.id]


async def test_workflow_cancel_takes_its_own_kind_and_passes_over_idle_subjects(
    druks_db, monkeypatch
):
    # Webhooks redeliver, and a PR can close long after its build ended: cancelling what
    # is already gone is the no-op the caller expects, not an error.
    subject = _work_item(remote_key="ENG-748-C")
    build = _subject_run(druks_db, subject=subject, kind=Build.kind, state="running")
    _subject_run(druks_db, subject=subject, kind=Profile.kind, state="running", order=1)
    cancelled = []

    async def cancel(self, *, failure=None):
        cancelled.append(self.id)

    monkeypatch.setattr(Run, "cancel", cancel)

    await Build.cancel(subject)
    assert cancelled == [build.id]

    idle = _work_item(remote_key="ENG-748-D")
    _subject_run(druks_db, subject=idle, kind=Build.kind, state="finished")
    await Build.cancel(idle)
    assert cancelled == [build.id]


async def test_subject_phase_reads_the_driving_running_workflow(druks_db, monkeypatch):
    subject = _work_item(remote_key="ENG-748-E")
    _subject_run(
        druks_db,
        subject=subject,
        kind=Build.kind,
        state="parked",
        gate=OperatorReply.name,
    )
    driving = _subject_run(druks_db, subject=subject, kind=Profile.kind, state="running", order=1)
    seen = []

    async def phase(workflow_id):
        seen.append(workflow_id)
        return "agent_running"

    monkeypatch.setattr("druks.durable.reads.get_run_phase", phase)

    assert await subject.get_phase() == "agent_running"
    assert seen == [driving.id]

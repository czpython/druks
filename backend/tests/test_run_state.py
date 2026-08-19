from datetime import UTC, datetime, timedelta
from unittest import mock

import pytest
from dbos._error import DBOSWorkflowCancelledError
from druks.database import db_session as ambient_session
from druks.durable.dbos_state import workflow_status
from druks.durable.enums import RunState
from druks.durable.models import Run
from druks.events.models import Event
from druks.models import Base
from druks.signals import subscribe
from druks.testing import seed_run
from druks.workflows import Workflow, WorkflowEvent, _emit_run_event, _execute_run
from druks_field_notes.models import Note
from druks_field_notes.workflows import Summarize
from sqlalchemy import select, update
from uuid_utils import uuid7


def _item_and_run(druks_db, state, **kwargs):
    note = Note.create(body=f"run in {state}")
    return note, seed_run(druks_db, kind=Summarize.kind, subject=note, state=state, **kwargs)


def test_session_get_derives_state(druks_db):
    _, run = _item_and_run(druks_db, "finished")
    druks_db.expire_all()
    assert Run.get(run.id).state == RunState.FINISHED.value


def test_pending_splits_on_the_gate(druks_db):
    # DBOS says PENDING either way; the gate is the one fact it can't know.
    _, parked = _item_and_run(druks_db, "parked", input_gate="review_work")
    _, live = _item_and_run(druks_db, "running")
    druks_db.expire_all()
    assert Run.get(parked.id).state == RunState.PARKED.value
    assert Run.get(live.id).state == RunState.RUNNING.value


def _rowless_run(session):
    """A run with no ``dbos.workflow_status`` row — the gap these tests are about,
    which ``seed_run`` closes by design."""
    run = Run(id=str(uuid7()), kind=Summarize.kind, account_id="system")
    session.add(run)
    session.flush()
    return run


def test_fresh_run_without_a_dbos_row_reads_scheduled(druks_db):
    # start() writes the row before DBOS commits the enqueue; inside that gap a
    # brand-new run legitimately has no workflow_status row and reads scheduled.
    run = _rowless_run(druks_db)
    druks_db.expire_all()
    assert Run.get(run.id).state == RunState.SCHEDULED.value


def test_run_without_a_dbos_row_past_grace_reads_orphaned(druks_db):
    # A run still rowless past the grace window won't start — its DBOS row is
    # gone (system tables wiped, or the executor destroyed) — so derived state
    # reads orphaned instead of scheduled forever.
    run = _rowless_run(druks_db)
    run.created_at = Base.utc_now() - timedelta(minutes=10)
    druks_db.flush()
    druks_db.expire_all()
    assert Run.get(run.id).state == RunState.ORPHANED.value


def test_unknown_dbos_status_reads_running(druks_db):
    # A DBOS status this mapping predates must not crash reads.
    _, run = _item_and_run(druks_db, "running")
    druks_db.execute(
        update(workflow_status)
        .where(workflow_status.c.workflow_uuid == run.id)
        .values(status="SOME_FUTURE_STATUS")
    )
    druks_db.expire_all()
    assert Run.get(run.id).state == RunState.RUNNING.value


@pytest.mark.parametrize(
    ("status", "state"),
    [
        (None, RunState.SCHEDULED),  # status column is nullable in DBOS's DDL
        ("DELAYED", RunState.SCHEDULED),
        ("MAX_RECOVERY_ATTEMPTS_EXCEEDED", RunState.FAILED),
    ],
)
def test_statuses_the_seed_map_never_writes(druks_db, status, state):
    _, run = _item_and_run(druks_db, "running")
    druks_db.execute(
        update(workflow_status)
        .where(workflow_status.c.workflow_uuid == run.id)
        .values(status=status)
    )
    druks_db.expire_all()
    assert Run.get(run.id).state == state.value


def test_queries_filter_on_derived_state(druks_db):
    _, parked = _item_and_run(druks_db, "parked", input_gate="review_work")
    _, done = _item_and_run(druks_db, "finished")
    ids = set(
        druks_db.scalars(
            select(Run.id).where(
                Run.id.in_([parked.id, done.id]),
                Run.state.in_([RunState.PARKED.value, RunState.RUNNING.value]),
            )
        )
    )
    assert ids == {parked.id}


def test_updated_at_folds_in_the_dbos_write(druks_db):
    # DBOS stamps its updated_at in epoch milliseconds; the derived updated_at
    # converts it and wins over creation and the parked ask.
    _, run = _item_and_run(druks_db, "finished")
    later_ms = int(datetime(2031, 1, 2, 3, 4, 5, tzinfo=UTC).timestamp() * 1000)
    druks_db.execute(
        update(workflow_status)
        .where(workflow_status.c.workflow_uuid == run.id)
        .values(updated_at=later_ms)
    )
    druks_db.expire_all()
    row = Run.get(run.id)
    assert row.updated_at == datetime(2031, 1, 2, 3, 4, 5, tzinfo=UTC)
    assert row.updated_at > row.created_at


@pytest.fixture
def _inline_steps():
    # Run each durable step inline — these tests exercise _emit_run_event's step
    # split and _execute_run's exception routing, not DBOS's checkpointing.
    async def run_inline(_options, fn):
        return await fn()

    with mock.patch("druks.workflows.DBOS.run_step_async", side_effect=run_inline):
        yield


@pytest.mark.asyncio
async def test_facts_and_event_land_before_a_raising_subscriber(druks_db, _inline_steps):
    # The fact write and its event commit before the signal fires, so a raising
    # subscriber can't roll them back. The failure itself still propagates:
    # delivery is at-least-once.
    item, run = _item_and_run(druks_db, "running")

    @subscribe(WorkflowEvent.PARKED, run=run.id)
    async def _raises(**_: object) -> None:
        raise RuntimeError("tracker down")

    with pytest.raises(RuntimeError, match="tracker down"):
        await _emit_run_event(
            run.id,
            RunState.PARKED,
            subject={"type": "work_item", "id": item.id},
            facts={"input_gate": "review_work", "input_request": {"label": "Review"}},
        )

    ambient_session().expire_all()
    row = Run.get(run.id)
    assert row.input_gate == "review_work"
    events = (
        ambient_session()
        .query(Event)
        .filter_by(type="workflow.parked", subject_id=str(item.id))
        .all()
    )
    assert len(events) == 1
    assert events[0].payload["gate"] == "review_work"


@pytest.mark.asyncio
async def test_lifecycle_subscribers_get_the_payload_before_dbos_commits(druks_db, _inline_steps):
    # The workflow.finished signal fires from inside the still-PENDING workflow —
    # derived state hasn't turned yet, which is why subscribers read the
    # payload, never Run.state. The body gets the run's own facts, never the
    # routing keys the filters match on.
    item, run = _item_and_run(druks_db, "running")
    seen: list[tuple[str, dict]] = []

    @subscribe(WorkflowEvent.FINISHED, run=run.id)
    async def _reads_the_payload(**payload: object) -> None:
        seen.append((Run.get(run.id).state, payload))

    await _emit_run_event(
        run.id,
        RunState.FINISHED,
        subject={"type": "work_item", "id": item.id},
        result={"status": "ok"},
    )

    ((state_at_signal, payload),) = seen
    assert state_at_signal == RunState.RUNNING.value
    assert payload["subject"] == {"type": "work_item", "id": item.id}
    assert payload["result"] == {"status": "ok"}
    assert "run" not in payload and "kind" not in payload


@pytest.mark.asyncio
async def test_cancellation_passes_through_untouched(druks_db, _inline_steps):
    # Operator cancel already carries its own reason and terminal status; the
    # body's cancellation exception must reach DBOS without a workflow.failed event
    # or a failure overwrite.
    item, run = _item_and_run(druks_db, "running")

    async def body() -> None:
        raise DBOSWorkflowCancelledError(f"workflow {run.id} cancelled")

    with pytest.raises(DBOSWorkflowCancelledError):
        await _execute_run(run.id, run.kind, {"type": "work_item", "id": item.id}, None, body)

    ambient_session().expire_all()
    assert Run.get(run.id).failure is None
    types = [
        e.type for e in ambient_session().query(Event).filter_by(subject_id=str(item.id)).all()
    ]
    assert "workflow.failed" not in types


@pytest.mark.asyncio
async def test_failure_writes_the_reason_and_reraises(druks_db, _inline_steps):
    # Both FatalError and a crash take this path: reason + workflow.failed land (the
    # gate pair cleared with them, so a failed run never keeps a stale ask),
    # then the exception reaches DBOS so it records the terminal ERROR that
    # derived state reads.
    from druks.durable.exceptions import FatalError

    item, run = _item_and_run(
        druks_db,
        "parked",
        input_gate="review_work",
        input_request={"label": "Review"},
    )

    async def body() -> None:
        raise FatalError("closed at review")

    with pytest.raises(FatalError):
        await _execute_run(run.id, run.kind, {"type": "work_item", "id": item.id}, None, body)

    ambient_session().expire_all()
    row = Run.get(run.id)
    assert row.failure == "closed at review"
    # A bare FatalError carries no distinguishing code — only its message.
    assert row.failure_code == ""
    assert row.input_gate is None
    assert row.input_request is None
    failed = (
        ambient_session()
        .query(Event)
        .filter_by(type="workflow.failed", subject_id=str(item.id))
        .one()
    )
    assert failed.payload["failure"] == "closed at review"


@pytest.mark.asyncio
async def test_gate_timeout_stamps_its_failure_code(druks_db, _inline_steps):
    # A gate timeout stamps its code beside the reason so read-sides can tell an
    # unanswered gate from a crash without parsing the failure text.
    from druks.durable.exceptions import GateTimeout

    item, run = _item_and_run(druks_db, "running")

    async def body() -> None:
        raise GateTimeout("review_work")

    with pytest.raises(GateTimeout):
        await _execute_run(run.id, run.kind, {"type": "work_item", "id": item.id}, None, body)

    ambient_session().expire_all()
    assert Run.get(run.id).failure_code == "gate_timeout"


@pytest.mark.asyncio
async def test_a_harness_failure_stamps_its_code(druks_db, _inline_steps):
    from druks.harnesses.exceptions import HarnessOverloadedError

    item, run = _item_and_run(druks_db, "running")

    async def body() -> None:
        raise HarnessOverloadedError("claude exited with 1. API Error: 529 Overloaded.")

    with pytest.raises(HarnessOverloadedError):
        await _execute_run(run.id, run.kind, {"type": "work_item", "id": item.id}, None, body)

    ambient_session().expire_all()
    assert Run.get(run.id).failure_code == "overloaded"


@pytest.mark.asyncio
async def test_an_exhausted_provisioning_failure_stamps_its_code(druks_db, _inline_steps):
    # An exhausted transient provisioning failure records the classified
    # ``sandbox_provisioning`` code rather than the empty string a raw drukbox
    # SDK exception used to leave behind — so the dashboard/taxonomy can name it.
    from druks.harnesses.exceptions import HarnessSandboxProvisioningError

    item, run = _item_and_run(druks_db, "running")

    async def body() -> None:
        raise HarnessSandboxProvisioningError("exe.dev VM creation timed out")

    with pytest.raises(HarnessSandboxProvisioningError):
        await _execute_run(run.id, run.kind, {"type": "work_item", "id": item.id}, None, body)

    ambient_session().expire_all()
    assert Run.get(run.id).failure_code == "sandbox_provisioning"


@pytest.mark.asyncio
async def test_a_foreign_code_never_becomes_the_failure_code(druks_db, _inline_steps):
    """``code`` is a common attribute name — asyncssh's is an int — so only
    the declaring families stamp the run; anything else records a crash."""
    import asyncssh

    item, run = _item_and_run(druks_db, "running")

    async def body() -> None:
        raise asyncssh.PermissionDenied("denied")

    with pytest.raises(asyncssh.PermissionDenied):
        await _execute_run(run.id, run.kind, {"type": "work_item", "id": item.id}, None, body)

    ambient_session().expire_all()
    assert Run.get(run.id).failure_code == ""


@pytest.mark.asyncio
async def test_announce_carries_the_runs_routing(druks_db):
    # The body states its facts; the platform injects what subscribers filter on,
    # and the publish rides its own named checkpoint — the boundary that keeps a
    # recovery replay from re-firing it.
    workflow = Workflow()
    workflow._subject = {"type": "note", "id": 7}
    received = []
    checkpoints = []

    @subscribe("test.announced", kind=workflow.kind)
    async def _receive(*, subject: dict, **facts: object) -> None:
        received.append((subject, facts))

    async def run_inline(options, fn):
        checkpoints.append(options["name"])
        return await fn()

    with mock.patch("druks.workflows.DBOS.run_step_async", side_effect=run_inline):
        await workflow.announce("test.announced", pr_number=12)

    assert received == [({"type": "note", "id": 7}, {"pr_number": 12})]
    assert checkpoints == ["test.announced"]


@pytest.mark.asyncio
async def test_announce_refuses_inside_a_step():
    from druks.durable.exceptions import WorkflowError
    from druks.workflows import _in_step

    workflow = Workflow()
    token = _in_step.set(True)
    try:
        with pytest.raises(WorkflowError, match="workflow body"):
            await workflow.announce("test.announced", pr_number=12)
    finally:
        _in_step.reset(token)

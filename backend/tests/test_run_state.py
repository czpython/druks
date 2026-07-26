from datetime import UTC, datetime, timedelta
from unittest import mock

import druks.contrib.ship.workflows  # noqa: F401  # registers ship.build, the seeded kind
import pytest
from conftest import make_test_work_item, seed_build_run, seed_run
from dbos._error import DBOSWorkflowCancelledError
from druks.database import db_session as ambient_session
from druks.durable.dbos_state import workflow_status
from druks.durable.enums import RunState
from druks.durable.models import Run
from druks.events.models import Event
from druks.models import Base
from druks.signals import subscribe
from druks.workflows import WorkflowEvent, _emit_run_event, _execute_run
from sqlalchemy import select, update
from uuid_utils import uuid7


def _item_and_run(db_session, state, **kwargs):
    item = make_test_work_item(repo="ClawHaven/acme-app", title=f"run in {state}")
    return item, seed_build_run(db_session, work_item_id=item.id, state=state, **kwargs)


def test_session_get_derives_state(db_session):
    _, run = _item_and_run(db_session, "finished")
    db_session.expire_all()
    assert Run.get(run.id).state == RunState.FINISHED.value


def test_pending_splits_on_the_gate(db_session):
    # DBOS says PENDING either way; the gate is the one fact it can't know.
    _, parked = _item_and_run(db_session, "pending_input", input_gate="review_work")
    _, live = _item_and_run(db_session, "running")
    db_session.expire_all()
    assert Run.get(parked.id).state == RunState.PENDING_INPUT.value
    assert Run.get(live.id).state == RunState.RUNNING.value


def test_fresh_run_without_a_dbos_row_reads_scheduled(db_session):
    # start() writes the row before DBOS commits the enqueue; inside that gap a
    # brand-new run legitimately has no workflow_status row and reads scheduled.
    run = seed_run(db_session, str(uuid7()))
    db_session.expire_all()
    assert Run.get(run.id).state == RunState.SCHEDULED.value


def test_run_without_a_dbos_row_past_grace_reads_orphaned(db_session):
    # A run still rowless past the grace window won't start — its DBOS row is
    # gone (system tables wiped, or the executor destroyed) — so derived state
    # reads orphaned instead of scheduled forever.
    run = seed_run(db_session, str(uuid7()))
    run.created_at = Base.utc_now() - timedelta(minutes=10)
    db_session.flush()
    db_session.expire_all()
    assert Run.get(run.id).state == RunState.ORPHANED.value


def test_unknown_dbos_status_reads_running(db_session):
    # A DBOS status this mapping predates must not crash reads.
    _, run = _item_and_run(db_session, "running")
    db_session.execute(
        update(workflow_status)
        .where(workflow_status.c.workflow_uuid == run.id)
        .values(status="SOME_FUTURE_STATUS")
    )
    db_session.expire_all()
    assert Run.get(run.id).state == RunState.RUNNING.value


@pytest.mark.parametrize(
    ("status", "state"),
    [
        (None, RunState.SCHEDULED),  # status column is nullable in DBOS's DDL
        ("DELAYED", RunState.SCHEDULED),
        ("MAX_RECOVERY_ATTEMPTS_EXCEEDED", RunState.FAILED),
    ],
)
def test_statuses_the_seed_map_never_writes(db_session, status, state):
    _, run = _item_and_run(db_session, "running")
    db_session.execute(
        update(workflow_status)
        .where(workflow_status.c.workflow_uuid == run.id)
        .values(status=status)
    )
    db_session.expire_all()
    assert Run.get(run.id).state == state.value


def test_queries_filter_on_derived_state(db_session):
    _, parked = _item_and_run(db_session, "pending_input", input_gate="review_work")
    _, done = _item_and_run(db_session, "finished")
    ids = set(
        db_session.scalars(
            select(Run.id).where(
                Run.id.in_([parked.id, done.id]),
                Run.state.in_([RunState.PENDING_INPUT.value, RunState.RUNNING.value]),
            )
        )
    )
    assert ids == {parked.id}


def test_updated_at_folds_in_the_dbos_write(db_session):
    # DBOS stamps its updated_at in epoch milliseconds; the derived updated_at
    # converts it and wins over creation and the parked ask.
    _, run = _item_and_run(db_session, "finished")
    later_ms = int(datetime(2031, 1, 2, 3, 4, 5, tzinfo=UTC).timestamp() * 1000)
    db_session.execute(
        update(workflow_status)
        .where(workflow_status.c.workflow_uuid == run.id)
        .values(updated_at=later_ms)
    )
    db_session.expire_all()
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
async def test_facts_and_event_land_before_a_raising_subscriber(db_session, _inline_steps):
    # The fact write and its event commit before the signal fires, so a raising
    # subscriber can't roll them back. The failure itself still propagates:
    # delivery is at-least-once.
    item, run = _item_and_run(db_session, "running")

    @subscribe(WorkflowEvent.PARKED, run=run.id)
    async def _raises(**_: object) -> None:
        raise RuntimeError("tracker down")

    with pytest.raises(RuntimeError, match="tracker down"):
        await _emit_run_event(
            run.id,
            RunState.PENDING_INPUT,
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
async def test_lifecycle_subscribers_get_the_payload_before_dbos_commits(db_session, _inline_steps):
    # The workflow.finished signal fires from inside the still-PENDING workflow —
    # derived state hasn't turned yet, which is why subscribers read the
    # payload, never Run.state. The body gets the run's own facts, never the
    # routing keys the filters match on.
    item, run = _item_and_run(db_session, "running")
    seen: list[tuple[str, dict]] = []

    @subscribe(WorkflowEvent.FINISHED, run=run.id)
    async def _reads_both(**kwargs: object) -> None:
        seen.append((Run.get(run.id).state, kwargs))

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
async def test_cancellation_passes_through_untouched(db_session, _inline_steps):
    # Operator cancel already carries its own reason and terminal status; the
    # body's cancellation exception must reach DBOS without a workflow.failed event
    # or a failure overwrite.
    item, run = _item_and_run(db_session, "running")

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
async def test_failure_writes_the_reason_and_reraises(db_session, _inline_steps):
    # Both FatalError and a crash take this path: reason + workflow.failed land (the
    # gate pair cleared with them, so a failed run never keeps a stale ask),
    # then the exception reaches DBOS so it records the terminal ERROR that
    # derived state reads.
    from druks.durable.exceptions import FatalError

    item, run = _item_and_run(
        db_session,
        "pending_input",
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
async def test_gate_timeout_stamps_its_failure_code(db_session, _inline_steps):
    # A gate timeout stamps its code beside the reason so read-sides can tell an
    # unanswered gate from a crash without parsing the failure text.
    from druks.durable.exceptions import GateTimeout

    item, run = _item_and_run(db_session, "running")

    async def body() -> None:
        raise GateTimeout("review_work")

    with pytest.raises(GateTimeout):
        await _execute_run(run.id, run.kind, {"type": "work_item", "id": item.id}, None, body)

    ambient_session().expire_all()
    assert Run.get(run.id).failure_code == "gate_timeout"

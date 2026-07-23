from unittest.mock import AsyncMock

import druks.build.subscribers  # noqa: F401 — registers the lane reactions
import pytest
from conftest import make_test_work_item, seed_dbos_status
from druks.build.enums import HandoffStatus
from druks.build.models import WorkItem
from druks.build.workflows import Scope, ScopeReply
from druks.durable import Run
from druks.durable.dbos_state import workflow_status
from druks.events.models import Event
from druks.signals import publish
from druks.ticketing.enums import SemanticStatus
from sqlalchemy import func, select, update
from uuid_utils import uuid7

pytestmark = pytest.mark.asyncio


async def test_run_running_puts_item_back_on_board(db_session):
    item = make_test_work_item(repo="acme/widget", title="t", source="linear", remote_key="ACME-1")
    item.set_status(HandoffStatus.SCOPED)

    await publish("run.running", subject=WorkItem.subject_for(item.id), kind="build.scope")

    assert WorkItem.get(item.id).status is None


async def test_scope_finish_settles_the_lane(db_session):
    item = make_test_work_item(repo="acme/widget", title="t", source="linear", remote_key="ACME-2")

    await publish(
        "run.finished",
        subject=WorkItem.subject_for(item.id),
        kind=Scope.kind,
        result={"status": "ready"},
    )

    assert WorkItem.get(item.id).status == HandoffStatus.SCOPED


async def test_parked_statuses_leave_the_item_active(db_session):
    item = make_test_work_item(repo="acme/widget", title="t", source="linear", remote_key="ACME-3")

    await publish(
        "run.finished",
        subject=WorkItem.subject_for(item.id),
        kind=Scope.kind,
        result={"status": "needs_answers"},
    )

    assert WorkItem.get(item.id).status is None


async def test_other_kinds_are_ignored(db_session):
    item = make_test_work_item(repo="acme/widget", title="t", source="linear", remote_key="ACME-4")
    item.set_status(HandoffStatus.SHIPPED)

    await publish(
        "run.finished",
        subject=WorkItem.subject_for(item.id),
        kind="build.build_workflow",
        result={"status": "ready"},
    )

    assert WorkItem.get(item.id).status == HandoffStatus.SHIPPED


async def test_build_lifecycle_reaches_the_tracker(db_session, monkeypatch):
    from druks.build.workflows import BuildWorkflow

    pushed = []

    async def _push(self, status):
        pushed.append(status)

    monkeypatch.setattr(WorkItem, "set_remote_status", _push)
    item = make_test_work_item(repo="acme/widget", title="t", source="linear", remote_key="ACME-7")
    subject = WorkItem.subject_for(item.id)

    await publish("run.running", subject=subject, kind=BuildWorkflow.kind)
    await publish("run.pending_input", subject=subject, kind=BuildWorkflow.kind, gate="review_work")
    # Other gates and other kinds don't push.
    await publish("run.pending_input", subject=subject, kind=BuildWorkflow.kind, gate="review_plan")
    await publish("run.running", subject=subject, kind="build.scope")

    assert pushed == [SemanticStatus.IN_PROGRESS, SemanticStatus.IN_REVIEW]


def _parked_scope_run(session, *, work_item_id):
    run = Run(
        id=str(uuid7()),
        kind=Scope.kind,
        input_gate=ScopeReply.name,
        input_request={"presentation": "external", "label": "Answer scope questions"},
    )
    session.add(run)
    session.flush()
    seed_dbos_status(
        session, run.id, "pending_input", subject={"type": "work_item", "id": work_item_id}
    )
    return run


def _cancelled_milestones(session, work_item_id):
    return session.scalar(
        select(func.count())
        .select_from(Event)
        .where(
            Event.subject_type == "work_item",
            Event.subject_id == str(work_item_id),
            Event.type == "cancelled",
        )
    )


async def test_ticket_close_cancels_the_parked_scope(db_session, monkeypatch):
    """Closing the ticket ends its parked scope now — cancelled run, cancelled
    lane, milestone recorded — instead of at the 14-day gate TTL."""
    cancelled = []

    async def _dbos_cancel(workflow_id):
        cancelled.append(workflow_id)
        db_session.execute(
            update(workflow_status)
            .where(workflow_status.c.workflow_uuid == workflow_id)
            .values(status="CANCELLED")
        )

    monkeypatch.setattr("dbos.DBOS.cancel_workflow_async", _dbos_cancel)
    item = make_test_work_item(repo="acme/widget", title="t", source="linear", remote_key="ACME-5")
    run = _parked_scope_run(db_session, work_item_id=item.id)

    await publish(
        "ticket.transitioned",
        payload={"source": "linear", "identifier": "ACME-5", "status": "Done", "terminal": True},
    )

    # Run.cancel() never writes state — re-select before reading the derived one.
    db_session.expire_all()
    refreshed = Run.get(run.id)
    assert refreshed
    assert refreshed.state == "cancelled"
    assert refreshed.input_gate is None
    assert refreshed.failure == "ticket closed while scope parked"
    assert item.status == HandoffStatus.CANCELLED
    assert _cancelled_milestones(db_session, item.id) == 1
    assert cancelled == [run.id]


async def test_ticket_close_without_a_parked_scope_changes_nothing(db_session, monkeypatch):
    """A closed ticket whose scope already resolved (or never ran) has nothing
    to cancel — the item's lane stays as it was."""
    cancel = AsyncMock()
    monkeypatch.setattr("dbos.DBOS.cancel_workflow_async", cancel)
    item = make_test_work_item(repo="acme/widget", title="t", source="linear", remote_key="ACME-6")

    await publish(
        "ticket.transitioned",
        payload={"source": "linear", "identifier": "ACME-6", "status": "Done", "terminal": True},
    )

    assert item.status is None
    assert _cancelled_milestones(db_session, item.id) == 0
    cancel.assert_not_called()


async def test_ticket_close_for_an_unknown_ticket_is_ignored(db_session):
    """A close for a ticket druks never scoped is someone else's business."""
    await publish(
        "ticket.transitioned",
        payload={"source": "linear", "identifier": "NOPE-1", "status": "Done", "terminal": True},
    )

    assert not db_session.scalar(select(func.count()).select_from(Event))


async def test_provision_state_reaches_the_work_item(db_session):
    from druks.build.workflows import BuildWorkflow

    item = make_test_work_item(repo="acme/widget", title="t", source="linear", remote_key="ACME-8")

    await publish(
        "run.state",
        subject=WorkItem.subject_for(item.id),
        kind=BuildWorkflow.kind,
        pr_number=12,
        branch="agent/eng-8",
        ticket_ref="ACME-8",
        issue_number=None,
    )

    refreshed = WorkItem.get(item.id)
    assert refreshed.pr_number == 12 and refreshed.branch == "agent/eng-8"

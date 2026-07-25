import druks.build.subscribers  # noqa: F401 — registers the lane reactions
import pytest
from conftest import make_test_work_item, seed_dbos_status
from druks.build.contracts import ReviewWork
from druks.build.enums import HandoffStatus
from druks.build.models import WorkItem
from druks.build.workflows import BuildWorkflow
from druks.durable import Run
from druks.signals import publish
from druks.ticketing.enums import TicketStatus
from uuid_utils import uuid7

pytestmark = pytest.mark.asyncio


async def test_run_running_puts_item_back_on_board(db_session):
    item = make_test_work_item(repo="acme/widget", title="t", source="linear", remote_key="ACME-1")
    item.set_status(HandoffStatus.CANCELLED)

    await publish("run.running", subject=item.identity, kind=BuildWorkflow.kind)

    assert WorkItem.get(item.id).status is None


async def test_build_lifecycle_reaches_the_tracker(db_session, monkeypatch):
    pushed = []

    async def _push(self, status):
        pushed.append(status)

    monkeypatch.setattr(WorkItem, "set_remote_status", _push)
    item = make_test_work_item(repo="acme/widget", title="t", source="linear", remote_key="ACME-7")
    subject = item.identity

    await publish("run.running", subject=subject, kind=BuildWorkflow.kind)
    await publish("run.pending_input", subject=subject, kind=BuildWorkflow.kind, gate="review_work")
    # Other gates and other kinds don't push.
    await publish("run.pending_input", subject=subject, kind=BuildWorkflow.kind, gate="review_plan")
    await publish("run.running", subject=subject, kind="build.profile")

    assert pushed == [TicketStatus.IN_PROGRESS, TicketStatus.IN_REVIEW]


async def test_pr_review_answers_through_the_review_gate(db_session, monkeypatch):
    item = make_test_work_item(repo="acme/widget", title="t", source="linear", remote_key="ACME-9")
    item.update(pr_number=12, branch="agent/acme-9")
    run = Run(
        id=str(uuid7()),
        kind=BuildWorkflow.kind,
        input_gate=ReviewWork.name,
        input_request={"presentation": "external", "label": "Review implementation"},
    )
    db_session.add(run)
    db_session.flush()
    seed_dbos_status(
        db_session,
        run.id,
        "pending_input",
        subject=item.identity,
    )
    item.update(build_run_id=run.id)
    answers = []

    async def answer(subject, **reply):
        answers.append((subject, reply))

    monkeypatch.setattr(ReviewWork, "answer", answer)

    await publish(
        "pr.review_submitted",
        repo=item.repo,
        pr_number=item.pr_number,
        payload={
            "branch": item.branch,
            "action": "request_changes",
            "reviewer": "alice",
            "body": "Please split the migration.",
        },
    )

    assert answers == [
        (
            item,
            {
                "action": "request_changes",
                "reviewer": "alice",
                "body": "Please split the migration.",
            },
        )
    ]


async def test_provision_state_reaches_the_work_item(db_session):
    item = make_test_work_item(repo="acme/widget", title="t", source="linear", remote_key="ACME-8")

    await publish(
        "run.state",
        subject=item.identity,
        kind=BuildWorkflow.kind,
        pr_number=12,
        branch="agent/eng-8",
        ticket_ref="ACME-8",
        issue_number=None,
    )

    refreshed = WorkItem.get(item.id)
    assert refreshed.pr_number == 12 and refreshed.branch == "agent/eng-8"

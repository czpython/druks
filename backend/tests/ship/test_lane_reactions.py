from datetime import UTC, datetime

import druks.contrib.ship.subscribers  # noqa: F401 — registers the lane reactions
import pytest
from druks.contrib.ship.contracts import ReviewWork
from druks.contrib.ship.models import WorkItem
from druks.contrib.ship.workflows import Build
from druks.durable import Run
from druks.durable.enums import WorkflowEvent
from druks.signals import publish
from druks.testing import seed_dbos_status
from druks.ticketing.enums import TicketStatus
from uuid_utils import uuid7

from ship.factories import make_test_work_item

pytestmark = pytest.mark.asyncio


async def test_new_build_claims_the_item(druks_db):
    # A resume replays the body without passing through start(), so this topic —
    # not RUNNING — is the one that fires once per attempt.
    item = make_test_work_item(repo="acme/widget", title="t", source="linear", ticket_key="ACME-1")
    item.update(pr_number=7, branch="agent/old")
    item.resolve(merged=False, at=datetime.now(UTC))

    await publish(WorkflowEvent.SCHEDULED, subject=item.identity, kind=Build.kind)

    refreshed = WorkItem.get(item.id)
    assert (refreshed.branch, refreshed.pr_number) == (None, None)
    assert (refreshed.resolution, refreshed.resolved_at) == (None, None)


async def test_build_lifecycle_reaches_the_tracker(druks_db, monkeypatch):
    pushed = []

    async def _push(self, status):
        pushed.append(status)

    monkeypatch.setattr(WorkItem, "set_ticket_status", _push)
    item = make_test_work_item(repo="acme/widget", title="t", source="linear", ticket_key="ACME-7")
    subject = item.identity

    await publish(WorkflowEvent.RUNNING, subject=subject, kind=Build.kind)
    await publish(WorkflowEvent.PARKED, subject=subject, kind=Build.kind, gate="review_work")
    # Other gates and other kinds don't push.
    await publish(WorkflowEvent.PARKED, subject=subject, kind=Build.kind, gate="review_plan")
    await publish(WorkflowEvent.RUNNING, subject=subject, kind="ship.profile")

    assert pushed == [TicketStatus.IN_PROGRESS, TicketStatus.IN_REVIEW]


async def test_pr_review_answers_through_the_review_gate(druks_db, monkeypatch):
    item = make_test_work_item(repo="acme/widget", title="t", source="linear", ticket_key="ACME-9")
    item.update(pr_number=12, branch="agent/acme-9")
    run = Run(
        id=str(uuid7()),
        kind=Build.kind,
        input_gate=ReviewWork.name,
        input_request={"presentation": "external", "label": "Review implementation"},
    )
    druks_db.add(run)
    druks_db.flush()
    seed_dbos_status(
        druks_db,
        run.id,
        "parked",
        subject=item.identity,
    )
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


async def test_pr_open_reaches_the_work_item(druks_db):
    item = make_test_work_item(repo="acme/widget", title="t", source="linear", ticket_key="ACME-8")

    await publish(
        "pr.opened",
        subject=item.identity,
        kind=Build.kind,
        pr_number=12,
        branch="agent/eng-8",
    )

    refreshed = WorkItem.get(item.id)
    assert refreshed.pr_number == 12 and refreshed.branch == "agent/eng-8"

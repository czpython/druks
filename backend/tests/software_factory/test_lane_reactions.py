from datetime import UTC, datetime
from unittest.mock import AsyncMock

import druks.contrib.software_factory.subscribers  # noqa: F401 — registers the lane reactions
import pytest
from druks.accounts.models import Account
from druks.contrib.software_factory.contracts import ReviewWork
from druks.contrib.software_factory.datastructures import PullRequest
from druks.contrib.software_factory.enums import Resolution
from druks.contrib.software_factory.models import WorkItem
from druks.contrib.software_factory.ticketing.enums import TicketStatus
from druks.contrib.software_factory.workflows import Build, PullRequestReview
from druks.durable import Run
from druks.durable.enums import WorkflowEvent
from druks.events.models import Event
from druks.signals import publish
from druks.testing import seed_dbos_status
from sqlalchemy import select
from uuid_utils import uuid7

from software_factory.factories import make_test_work_item

pytestmark = pytest.mark.asyncio


async def test_new_build_claims_the_item(druks_db):
    # A resume replays the body without passing through start(), so this topic —
    # not RUNNING — is the one that fires once per attempt.
    item = await make_test_work_item(
        repo="acme/widget", title="t", source="linear", ticket_key="ACME-1"
    )
    await item.update(pr_number=7, branch="agent/old")
    await item.resolve(Resolution.CLOSED, at=datetime.now(UTC))

    await publish(WorkflowEvent.SCHEDULED, subject=item.identity, kind=Build.kind)

    refreshed = await WorkItem.get_or_none(id=item.id)
    assert (refreshed.branch, refreshed.pr_number) == (None, None)
    assert (refreshed.resolution, refreshed.resolved_at) == (None, None)


async def test_cancelled_build_settles_the_item(druks_db):
    item = await make_test_work_item(
        repo="acme/widget", title="t", source="linear", ticket_key="ACME-2"
    )

    await publish(
        WorkflowEvent.CANCELLED,
        subject=item.identity,
        kind=Build.kind,
        failure="operator cancelled",
    )

    refreshed = await WorkItem.get_or_none(id=item.id)
    assert refreshed.resolution == Resolution.CANCELLED
    assert refreshed.resolved_at
    druks_db.expunge_all()
    assert str(item.id) not in {summary.id for summary in await WorkItem.list_summaries(None)}


@pytest.mark.parametrize("resolution", [Resolution.MERGED, Resolution.CLOSED])
async def test_cancelled_build_preserves_an_existing_resolution(druks_db, resolution):
    item = await make_test_work_item(
        repo="acme/widget", title="t", source="linear", ticket_key="ACME-3"
    )
    resolved_at = datetime(2026, 8, 8, 12, tzinfo=UTC)
    await item.resolve(resolution, at=resolved_at)

    await publish(
        WorkflowEvent.CANCELLED,
        subject=item.identity,
        kind=Build.kind,
        failure="operator cancelled",
    )

    refreshed = await WorkItem.get_or_none(id=item.id)
    assert refreshed.resolution == resolution
    assert refreshed.resolved_at == resolved_at


async def test_failed_build_remains_unresolved_on_the_board(druks_db):
    item = await make_test_work_item(
        repo="acme/widget", title="t", source="linear", ticket_key="ACME-4"
    )

    await publish(
        WorkflowEvent.FAILED,
        subject=item.identity,
        kind=Build.kind,
        failure="build failed",
    )

    refreshed = await WorkItem.get_or_none(id=item.id)
    assert (refreshed.resolution, refreshed.resolved_at) == (None, None)
    druks_db.expunge_all()
    assert str(item.id) in {summary.id for summary in await WorkItem.list_summaries(None)}


async def test_build_lifecycle_reaches_the_tracker(druks_db, monkeypatch):
    pushed = []

    async def _push(self, status):
        pushed.append(status)

    monkeypatch.setattr(WorkItem, "set_ticket_status", _push)
    item = await make_test_work_item(
        repo="acme/widget", title="t", source="linear", ticket_key="ACME-7"
    )
    subject = item.identity

    await publish(WorkflowEvent.RUNNING, subject=subject, kind=Build.kind)
    await publish(WorkflowEvent.PARKED, subject=subject, kind=Build.kind, gate="review_work")
    # Other gates and other kinds don't push.
    await publish(WorkflowEvent.PARKED, subject=subject, kind=Build.kind, gate="review_plan")
    await publish(WorkflowEvent.RUNNING, subject=subject, kind="software_factory.profile")

    assert pushed == [TicketStatus.IN_PROGRESS, TicketStatus.IN_REVIEW]


# On a public repo any GitHub account can approve; only a writer's review answers.
@pytest.mark.parametrize(
    ("action", "author_can_write"), [("request_changes", True), ("approve", False)]
)
async def test_pr_review_answers_through_the_review_gate(
    druks_db, monkeypatch, action, author_can_write
):
    item = await make_test_work_item(
        repo="acme/widget", title="t", source="linear", ticket_key="ACME-9"
    )
    await item.update(pr_number=12, branch="agent/acme-9")
    run = Run(
        account_id=(await Account.get_or_create(druks_db, "op@example.com")).id,
        id=str(uuid7()),
        kind=Build.kind,
        input_gate=ReviewWork.name,
        input_request={"presentation": "external", "label": "Review implementation"},
    )
    druks_db.add(run)
    await druks_db.flush()
    await seed_dbos_status(
        druks_db,
        run.id,
        "parked",
        subject=item.identity,
    )
    answers = []

    async def answer(subject, *, workflow, **reply):
        answers.append((subject, workflow, reply))

    monkeypatch.setattr(ReviewWork, "answer", answer)

    await publish(
        "pr.review_submitted",
        repo=item.repo,
        pr_number=item.pr_number,
        payload={
            "branch": item.branch,
            "action": action,
            "reviewer": "alice",
            "author_can_write": author_can_write,
            "body": "Please split the migration.",
        },
    )

    reply = {"action": action, "reviewer": "alice", "body": "Please split the migration."}
    assert answers == ([(item, Build, reply)] if author_can_write else [])


async def test_github_pr_open_records_the_pr_of_the_items_branch(druks_db):
    item = await make_test_work_item(
        repo="acme/widget", title="t", source="linear", ticket_key="ACME-8"
    )
    await item.update(branch="agent/ACME-8")

    # GitHub redelivers.
    for _ in range(2):
        await publish(
            "pr.opened",
            repo="acme/widget",
            pr_number=12,
            payload={"branch": "agent/ACME-8", "is_fork": False},
        )

    opened = await druks_db.scalars(
        select(Event).where(Event.type == "build.pr_opened", Event.subject_id == str(item.id))
    )
    assert item.pr_number == 12
    assert [event.payload for event in opened] == [{"repo": "acme/widget", "pr_number": 12}]


async def test_a_fork_pr_on_the_items_branch_leaves_the_item_alone(druks_db):
    item = await make_test_work_item(
        repo="acme/widget", title="t", source="linear", ticket_key="ACME-9"
    )
    await item.update(branch="agent/ACME-9")

    await publish(
        "pr.opened",
        repo="acme/widget",
        pr_number=13,
        payload={"branch": "agent/ACME-9", "is_fork": True},
    )

    assert not item.pr_number


async def test_merged_reply_keeps_done_before_resolution_commit(druks_db, monkeypatch):
    item = await make_test_work_item(repo="acme/widget", title="Merged")
    push = AsyncMock()
    monkeypatch.setattr(WorkItem, "set_ticket_status", push)

    await publish(
        WorkflowEvent.RUNNING,
        subject=item.identity,
        kind=Build.kind,
        result={"action": "merged"},
    )

    push.assert_not_awaited()


async def test_merge_before_park_answers_review(druks_db, monkeypatch):
    item = await make_test_work_item(repo="acme/widget", title="Merged before park")
    item.resolution = "merged"
    await druks_db.flush()
    push, answer = AsyncMock(), AsyncMock()
    monkeypatch.setattr(WorkItem, "set_ticket_status", push)
    monkeypatch.setattr(ReviewWork, "answer", answer)

    await publish(
        WorkflowEvent.PARKED,
        subject=item.identity,
        kind=Build.kind,
        gate=ReviewWork.name,
    )

    answer.assert_awaited_once_with(item, workflow=Build, action="merged")
    push.assert_not_awaited()


async def _answers_to_the_build(monkeypatch) -> list[tuple]:
    answers: list[tuple] = []

    async def answer(cls, subject, topic, **facts):
        answers.append((subject.id, topic, facts))

    monkeypatch.setattr(Build, "answer", classmethod(answer))
    return answers


async def _item_with_pr(ticket_key: str, pr_number: int) -> WorkItem:
    item = await make_test_work_item(
        repo="acme/widget", title="t", source="linear", ticket_key=ticket_key
    )
    await item.update(pr_number=pr_number, branch=f"agent/{ticket_key.lower()}")
    return item


async def test_a_finished_review_answers_the_build_of_its_pull_request(druks_db, monkeypatch):
    item = await _item_with_pr("ACME-30", 42)
    answers = await _answers_to_the_build(monkeypatch)
    result = {"decision": "approve", "summary": "Looks right.", "findings": [], "context_repos": []}

    await publish(
        WorkflowEvent.FINISHED,
        subject=PullRequest(id="acme/widget#42").identity,
        kind=PullRequestReview.kind,
        result=result,
    )

    assert answers == [(item.id, "review.completed", result)]


@pytest.mark.parametrize("event", [WorkflowEvent.FAILED, WorkflowEvent.CANCELLED])
async def test_a_stopped_review_fails_the_build_with_its_failure(druks_db, monkeypatch, event):
    item = await _item_with_pr("ACME-31", 43)
    answers = await _answers_to_the_build(monkeypatch)

    await publish(
        event,
        subject=PullRequest(id="acme/widget#43").identity,
        kind=PullRequestReview.kind,
        failure="druks-review has no access",
    )

    assert answers == [(item.id, "review.completed", {"failure": "druks-review has no access"})]


async def test_a_review_of_a_pull_request_no_build_owns_answers_no_one(druks_db, monkeypatch):
    answers = await _answers_to_the_build(monkeypatch)

    await publish(
        WorkflowEvent.FINISHED,
        subject=PullRequest(id="acme/widget#99").identity,
        kind=PullRequestReview.kind,
        result={"decision": "approve", "summary": "", "findings": [], "context_repos": []},
    )

    assert answers == []

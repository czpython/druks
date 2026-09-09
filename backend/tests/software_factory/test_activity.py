from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from conftest import installation_key
from druks.contrib.software_factory.contracts import (
    ContractRevisionOutput,
    FindingOutput,
    PlanOutput,
    ReviewReport,
)
from druks.contrib.software_factory.datastructures import PullRequest
from druks.contrib.software_factory.models import WorkItem
from druks.contrib.software_factory.subscribers import pr_close_settles_the_item
from druks.contrib.software_factory.workflows import Build, PullRequestReview
from druks.database import db_session
from druks.durable.models import AgentCall, Artifact
from druks.events.models import Event
from druks.testing import seed_run
from sqlalchemy import select

from software_factory.factories import make_test_work_item


def test_plan_outputs_declare_their_saved_results():
    plan = PlanOutput(
        plan_markdown="# Plan",
        acceptance_criteria=[],
        questions=[],
        rejected_approaches=[],
        confidence="high",
        assignee_github_login=None,
    )
    revision = ContractRevisionOutput(
        plan_markdown="# Revised plan",
        acceptance_criteria=[],
        implementation_instructions="Build it.",
    )
    assert plan.get_activity() == {"kind": "plan.prepared"}
    assert revision.get_activity() == {"kind": "plan.revised"}
    assert plan.get_artifact()["content"] == "# Plan"
    assert revision.get_artifact()["content"] == "# Revised plan"


async def test_review_result_belongs_to_the_identity_only_pull_request(druks_db, tmp_path):
    db_session.registry.set(druks_db)
    subject = PullRequest.get("acme/widget", 42)
    run = await seed_run(druks_db, kind=PullRequestReview.kind, subject=subject)
    key = await installation_key()
    call = AgentCall(
        id="factory-review",
        run_id=run.id,
        agent="software_factory.review_pull_request",
        model="test",
        sandbox_host_id="test",
        api_key_id=key.id,
    )
    druks_db.add(call)
    await druks_db.flush()
    druks_db.expunge_all()
    report = ReviewReport(
        decision="request_changes",
        summary="The write can lose data.",
        context_repos=[],
        findings=[
            FindingOutput(
                severity="high",
                summary="Keep the transaction open",
                evidence="The commit precedes the write.",
                path="backend/write.py",
                line=12,
                start_line=10,
            )
        ],
    )
    for _ in range(2):
        await Artifact.record(
            call_id=call.id,
            call_dir=tmp_path,
            activity=report.get_activity(),
            **report.get_artifact(),
        )
    artifact = await Artifact.get_for_call(call.id)
    content = (tmp_path / artifact.path).read_text()
    assert "request_changes" in content
    assert "The write can lose data." in content
    assert "## Keep the transaction open" in content
    assert "The commit precedes the write." in content
    assert "backend/write.py:10-12" in content
    events = list(await druks_db.scalars(select(Event)))
    assert len(events) == 1
    event = events[0]
    assert event.type == "review.completed"
    assert event.app == "software_factory"
    assert (event.subject_type, event.subject_id) == ("pull_request", "acme/widget#42")
    assert event.payload["artifact_id"] == artifact.id
    assert event.payload["agent_call_id"] == call.id
    assert event.payload["run"] == run.id
    assert subject.url == "https://github.com/acme/widget/pull/42"


async def test_owner_outcome_and_announcement_roll_back_together(druks_db, monkeypatch):
    db_session.registry.set(druks_db)
    item = await make_test_work_item(repo="acme/widget", title="Atomic outcome")
    item_id = item.id
    monkeypatch.setattr(Event, "emit", AsyncMock(side_effect=RuntimeError("Event insert failed")))
    with pytest.raises(RuntimeError, match="Event insert failed"):
        async with druks_db.begin_nested():
            await item.resolve(merged=True, at=datetime.now(UTC))
    druks_db.expunge_all()
    assert not (await WorkItem.get(item_id)).resolution
    assert not list(await druks_db.scalars(select(Event)))


async def test_stale_pr_on_a_reused_branch_cannot_resolve_the_current_attempt(druks_db):
    item = await make_test_work_item(repo="acme/widget", title="Current attempt")
    await item.update(pr_number=43, branch="agent/current")
    await pr_close_settles_the_item(
        repo=item.repo,
        pr_number=42,
        payload={"branch": item.branch, "merged": True, "resolved_at": datetime.now(UTC)},
    )
    assert not item.resolution


@pytest.mark.parametrize("pr_number", [None, 42])
async def test_operator_stop_records_no_owner_close(druks_db, druks_client, pr_number):
    item = await make_test_work_item(repo="acme/widget", title="Stopped work")
    await item.update(pr_number=pr_number, branch="agent/stopped")
    run = await seed_run(druks_db, kind=Build.kind, subject=item)
    for _ in range(2):
        response = await druks_client.post(
            f"/api/runs/{run.id}/cancel", json={"reason": "Operator stopped work"}
        )
        assert response.status_code == 200
    events = list(await druks_db.scalars(select(Event).where(Event.subject_id == str(item.id))))
    assert [event.type for event in events] == ["workflow.cancelled"]
    druks_db.expunge_all()
    assert (await WorkItem.get(item.id)).resolution == "closed"


@pytest.mark.parametrize("state", ["parked", "failed"])
async def test_owner_merge_records_once_without_an_operator_stop(druks_db, state):
    item = await make_test_work_item(repo="acme/widget", title="Merged work")
    await item.update(pr_number=42, branch="agent/merged")
    await seed_run(
        druks_db,
        kind=Build.kind,
        subject=item,
        state=state,
        input_gate="review_work" if state == "parked" else None,
    )
    for _ in range(2):
        await pr_close_settles_the_item(
            repo=item.repo,
            pr_number=42,
            payload={"branch": item.branch, "merged": True, "resolved_at": datetime.now(UTC)},
        )
    events = list(await druks_db.scalars(select(Event).where(Event.subject_id == str(item.id))))
    assert [event.type for event in events] == ["merged"]
    assert item.resolution == "merged"

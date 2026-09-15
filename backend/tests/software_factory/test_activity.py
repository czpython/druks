from datetime import UTC, datetime

import pytest
from conftest import installation_key
from druks.contrib.software_factory.app import SoftwareFactory
from druks.contrib.software_factory.contracts import (
    ContractRevisionOutput,
    FindingOutput,
    ImplementationOutput,
    PlanOutput,
    ReviewReport,
)
from druks.contrib.software_factory.datastructures import PullRequest
from druks.contrib.software_factory.enums import Resolution
from druks.contrib.software_factory.models import WorkItem
from druks.contrib.software_factory.subscribers import pr_close_settles_the_item
from druks.contrib.software_factory.workflows import Build, PullRequestReview
from druks.database import db_session
from druks.durable.models import AgentCall, Artifact
from druks.events.models import Event
from druks.testing import seed_run
from sqlalchemy import select

from software_factory.factories import make_test_work_item


async def test_first_delivery_records_its_pr_and_work_title(druks_db, druks_client, monkeypatch):
    item = await make_test_work_item(
        repo="acme/widget", title="Repair the queue", ticket_key="ACME-42"
    )
    run = await seed_run(druks_db, kind=Build.kind, subject=item)
    workflow = Build()
    workflow._workflow_id = run.id
    workflow._subject = item.identity
    delivery = ImplementationOutput(
        type="result",
        status="success",
        base_sha="base",
        head_sha="head",
        commit_sha="commit",
        branch="agent/queue",
        pr_number=42,
        files_changed=["queue.py"],
        acceptance_results=[],
        checks=[],
        known_risks=[],
        summary="Repaired the queue.",
        workspace_path="/work/queue",
        workspace_retention=None,
    )

    async def implement():
        workflow.journal.add(delivery)
        return delivery

    async def run_step(options, operation):
        return await operation()

    monkeypatch.setattr(SoftwareFactory, "implement", implement)
    monkeypatch.setattr("druks.workflows.DBOS.run_step_async", run_step)
    await item.update(title="A renamed queue")
    await workflow.implement()
    await workflow.implement()
    item = await WorkItem.get(item.id)
    assert item.pr_number == 42
    await item.start_attempt()
    await item.update(pr_number=43, branch="agent/next")

    response = await druks_client.get("/api/events?app=software_factory&topic=pr.opened")

    assert response.status_code == 200
    events = response.json()["items"]
    assert len(events) == 1
    assert events[0]["subjectLabel"] == "ACME-42"
    assert events[0]["payload"] == {
        "repo": "acme/widget",
        "pr_number": 42,
        "branch": "agent/queue",
        "title": "Repair the queue",
        "run": run.id,
        "kind": Build.kind,
    }


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
    assert plan.to_event() == {"topic": "plan.prepared"}
    assert revision.to_event() == {"topic": "plan.revised"}
    assert plan.to_artifact()["content"] == "# Plan"
    assert revision.to_artifact()["content"] == "# Revised plan"


async def test_review_result_belongs_to_the_identity_only_pull_request(
    druks_db, druks_client, tmp_path
):
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
            ),
            FindingOutput(
                severity="low",
                summary="Name the retry",
                evidence="The loop hides its purpose.",
                path="backend/write.py",
                line=5,
                start_line=None,
            ),
            FindingOutput(
                severity="low",
                summary="Describe the rollout",
                evidence="The change needs a note.",
                path=None,
                line=None,
                start_line=None,
            ),
        ],
    )
    for _ in range(2):
        await Artifact.record(
            druks_db,
            call_id=call.id,
            call_dir=tmp_path,
            event=report.to_event(),
            **report.to_artifact(),
        )
    artifact = await Artifact.get_for_call(call.id)
    content = (tmp_path / artifact.path).read_text()
    assert "request_changes" in content
    assert "The write can lose data." in content
    assert "## Keep the transaction open" in content
    assert "The commit precedes the write." in content
    assert "backend/write.py:10-12" in content
    assert "backend/write.py:5`" in content
    assert content.count("Source:") == 2
    events = list(await druks_db.scalars(select(Event)))
    assert len(events) == 1
    event = events[0]
    assert event.type == "review.completed"
    assert event.app == "software_factory"
    assert (event.subject_type, event.subject_id) == ("pull_request", "acme/widget#42")
    assert event.payload["artifact_id"] == artifact.id
    assert event.payload["agent_call_id"] == call.id
    assert event.payload["run"] == run.id
    assert event.payload["summary"] == report.summary
    response = await druks_client.get("/api/events?app=software_factory&topic=review.completed")
    assert response.status_code == 200
    assert response.json()["items"][0]["payload"] == event.payload


async def test_owner_outcome_and_announcement_roll_back_together(druks_db):
    db_session.registry.set(druks_db)
    item = await make_test_work_item(repo="acme/widget", title="Atomic outcome")
    await item.update(pr_number=42, branch="agent/atomic")
    item_id = item.id
    with pytest.raises(RuntimeError, match="Roll back the delivery"):
        async with druks_db.begin_nested():
            await pr_close_settles_the_item(
                repo=item.repo,
                pr_number=42,
                payload={"merged": True, "resolved_at": datetime.now(UTC)},
            )
            raise RuntimeError("Roll back the delivery")
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
    assert not list(await druks_db.scalars(select(Event)))


@pytest.mark.parametrize(("pr_number", "branch"), [(None, None), (42, "agent/stopped")])
async def test_operator_stop_records_no_owner_close(druks_db, druks_client, pr_number, branch):
    db_session.registry.set(druks_db)
    item = await make_test_work_item(repo="acme/widget", title="Stopped work")
    await item.update(pr_number=pr_number, branch=branch)
    run = await seed_run(druks_db, kind=Build.kind, subject=item)
    for _ in range(2):
        response = await druks_client.post(
            f"/api/runs/{run.id}/cancel", json={"reason": "Operator stopped work"}
        )
        assert response.status_code == 200
    events = list(await druks_db.scalars(select(Event).where(Event.subject_id == str(item.id))))
    assert [event.type for event in events] == ["workflow.cancelled"]
    druks_db.expunge_all()
    assert (await WorkItem.get(item.id)).resolution == Resolution.CANCELLED


@pytest.mark.parametrize("state", ["parked", "failed"])
@pytest.mark.parametrize("merged", [True, False])
async def test_owner_outcome_records_once_without_an_operator_stop(druks_db, state, merged):
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
            payload={"branch": item.branch, "merged": merged, "resolved_at": datetime.now(UTC)},
        )
    events = list(await druks_db.scalars(select(Event).where(Event.subject_id == str(item.id))))
    outcome = "merged" if merged else "closed"
    assert [event.type for event in events] == [outcome]
    assert events[0].payload == {"repo": "acme/widget", "pr_number": 42, "title": "Merged work"}
    assert item.resolution == outcome
    await item.start_attempt()
    await item.update(pr_number=43, branch="agent/next", title="Next attempt")
    druks_db.expunge_all()
    recorded = (await druks_db.scalars(select(Event).where(Event.id == events[0].id))).one()
    assert recorded.payload == {"repo": "acme/widget", "pr_number": 42, "title": "Merged work"}


async def test_owner_merge_replaces_an_operator_cancel(druks_db, druks_client):
    db_session.registry.set(druks_db)
    item = await make_test_work_item(repo="acme/widget", title="Merged after a cancel")
    await item.update(pr_number=42, branch="agent/cancelled")
    run = await seed_run(druks_db, kind=Build.kind, subject=item)
    response = await druks_client.post(
        f"/api/runs/{run.id}/cancel", json={"reason": "Operator stopped work"}
    )
    assert response.status_code == 200
    druks_db.expunge_all()
    await pr_close_settles_the_item(
        repo="acme/widget",
        pr_number=42,
        payload={"branch": "agent/cancelled", "merged": True, "resolved_at": datetime.now(UTC)},
    )
    events = list(await druks_db.scalars(select(Event).where(Event.subject_id == str(item.id))))
    assert [event.type for event in events] == ["workflow.cancelled", "merged"]
    assert events[1].payload == {
        "repo": "acme/widget",
        "pr_number": 42,
        "title": "Merged after a cancel",
    }
    druks_db.expunge_all()
    assert (await WorkItem.get(item.id)).resolution == Resolution.MERGED

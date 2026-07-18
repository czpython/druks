from types import SimpleNamespace

import druks.build.subscribers  # noqa: F401 — registers the pr.closed subscriber
import pytest
from conftest import make_settings, make_test_work_item, seed_build_run
from druks.build.models import WorkItem
from druks.core.webhooks.github import GitHubEvents
from druks.durable import Run
from druks.events.models import Event
from sqlalchemy import func, select


def _milestone_count(work_item_id, milestone):
    from druks.database import db_session

    return db_session().scalar(
        select(func.count())
        .select_from(Event)
        .where(
            Event.subject_type == "work_item",
            Event.subject_id == str(work_item_id),
            Event.type == milestone,
        )
    )


async def _fire_closed(*, repo, pr_number, branch, tmp_path, merged=True):
    payload = {
        "repository": {"full_name": repo},
        "pull_request": {
            "number": pr_number,
            "merged": merged,
            "head": {"ref": branch},
        },
    }
    events = GitHubEvents(
        request=SimpleNamespace(),
        kwargs={},
        settings=make_settings(tmp_path),
    )
    events._data_cached = payload
    await events.on_pull_request_closed()


def _park_work_item(*, repo, pr_number, branch, state="pending_input", input_gate="review_work"):
    """A work item with a build run paused on the operator (review_work) — the
    haunting case. Returns (work_item_id, build_run_id)."""
    from druks.database import db_session

    item = make_test_work_item(repo=repo, title="Externally merged")
    item.update(pr_number=pr_number, branch=branch)
    run = seed_build_run(
        db_session(),
        work_item_id=item.id,
        state=state,
        input_gate=input_gate if state == "pending_input" else None,
    )
    return item.id, run.id


def _fresh_run(run_id):
    # Run.cancel() never writes state — re-select before reading the derived one.
    from druks.database import db_session

    db_session().expire_all()
    return Run.get(run_id)


@pytest.mark.asyncio
async def test_external_merge_records_event_and_ends_involvement(db_session, tmp_path):
    repo, pr_number, branch = "ClawHaven/acme-app", 42, "agent/eng-1"
    work_item_id, run_id = _park_work_item(repo=repo, pr_number=pr_number, branch=branch)

    await _fire_closed(repo=repo, pr_number=pr_number, branch=branch, tmp_path=tmp_path)

    # Ship-ness is recorded as the 'shipped' milestone...
    assert _milestone_count(work_item_id, "shipped") == 1
    # ...and involvement ended: the parked build run is cancelled.
    assert not _fresh_run(run_id).is_active


@pytest.mark.asyncio
async def test_merge_ships_but_leaves_a_running_build_to_converge(db_session, tmp_path):
    """merged=True ships the item immediately — GitHub is the announcer for
    druks's own merges too. A RUNNING run is left alone: it converges on its
    own (its merge step sees the closed PR)."""
    repo, pr_number, branch = "ClawHaven/acme-app", 43, "agent/eng-2"
    work_item_id, run_id = _park_work_item(
        repo=repo,
        pr_number=pr_number,
        branch=branch,
        state="running",
    )

    await _fire_closed(repo=repo, pr_number=pr_number, branch=branch, tmp_path=tmp_path)

    assert Run.get(run_id).state == "running"  # not cancelled from under druks
    assert _milestone_count(work_item_id, "shipped") == 1


@pytest.mark.asyncio
async def test_redelivered_merge_webhook_does_not_double_record(db_session, tmp_path):
    """GitHub redelivers webhooks; a shipped item stays shipped once."""
    repo, pr_number, branch = "ClawHaven/acme-app", 44, "agent/eng-3"
    work_item_id, _ = _park_work_item(repo=repo, pr_number=pr_number, branch=branch)
    await _fire_closed(repo=repo, pr_number=pr_number, branch=branch, tmp_path=tmp_path)

    await _fire_closed(repo=repo, pr_number=pr_number, branch=branch, tmp_path=tmp_path)

    assert _milestone_count(work_item_id, "shipped") == 1


@pytest.mark.asyncio
async def test_closed_unmerged_records_close_and_ends_involvement(db_session, tmp_path):
    """A PR closed *without* merging — the operator abandoned it (e.g. deleted
    the branch). Emit 'cancelled' and un-park so the item derives as cancelled
    and leaves the active board, rather than being ignored."""
    repo, pr_number, branch = "ClawHaven/acme-app", 45, "agent/eng-4"
    work_item_id, run_id = _park_work_item(repo=repo, pr_number=pr_number, branch=branch)

    await _fire_closed(
        repo=repo,
        pr_number=pr_number,
        branch=branch,
        tmp_path=tmp_path,
        merged=False,
    )

    assert _milestone_count(work_item_id, "cancelled") == 1
    assert _milestone_count(work_item_id, "shipped") == 0
    assert not _fresh_run(run_id).is_active


@pytest.mark.asyncio
async def test_closed_unmerged_cancels_in_flight_run(db_session, tmp_path):
    repo, pr_number, branch = "ClawHaven/acme-app", 46, "agent/eng-5"
    work_item_id, run_id = _park_work_item(
        repo=repo,
        pr_number=pr_number,
        branch=branch,
        state="running",
    )

    await _fire_closed(
        repo=repo, pr_number=pr_number, branch=branch, tmp_path=tmp_path, merged=False
    )

    assert _fresh_run(run_id).state == "cancelled"
    assert _milestone_count(work_item_id, "cancelled") == 1


@pytest.mark.asyncio
async def test_remerge_after_retrigger_records_a_fresh_shipped(db_session, tmp_path):
    """A prior round's 'shipped' must not swallow a second merge after the item
    was re-triggered — the lane is active again, so the webhook ships it again."""
    from datetime import UTC, datetime, timedelta

    from druks.database import db_session as ds

    repo, pr_number, branch = "ClawHaven/acme-app", 77, "agent/eng-9"
    work_item_id, _ = _park_work_item(repo=repo, pr_number=pr_number, branch=branch)
    # Round 1: shipped.
    WorkItem.get(work_item_id).set_status("shipped")
    # Re-trigger: a newer RUNNING build run; dispatch cleared the handoff status.
    new_run = seed_build_run(ds(), work_item_id=work_item_id, state="running")
    new_run.created_at = datetime.now(UTC) + timedelta(seconds=5)
    WorkItem.get(work_item_id).set_status(None)
    ds().flush()

    await _fire_closed(repo=repo, pr_number=pr_number, branch=branch, tmp_path=tmp_path)

    assert _milestone_count(work_item_id, "shipped") == 2


@pytest.mark.asyncio
async def test_merge_echo_with_no_newer_activity_still_dedups(db_session, tmp_path):
    """The echo case: druks's own merge emits 'shipped' and GitHub's closed
    webhook arrives with nothing newer — still dropped."""
    repo, pr_number, branch = "ClawHaven/acme-app", 78, "agent/eng-10"
    work_item_id, _ = _park_work_item(repo=repo, pr_number=pr_number, branch=branch)
    WorkItem.get(work_item_id).set_status("shipped")

    await _fire_closed(repo=repo, pr_number=pr_number, branch=branch, tmp_path=tmp_path)

    assert _milestone_count(work_item_id, "shipped") == 1  # deduped


@pytest.mark.asyncio
async def test_external_close_returns_ticket_to_resting_pool(db_session, tmp_path, monkeypatch):
    """Closing the PR abandons the attempt, not the ticket: druks pushes the
    provider's resting status (Linear → post-refinement, Jira → Open) so the
    ticket doesn't strand in In Progress/Review."""
    from druks.build.models import WorkItem
    from druks.ticketing.enums import SemanticStatus

    pushed = []

    async def _record(self, status):
        pushed.append((self.id, status))

    monkeypatch.setattr(WorkItem, "set_remote_status", _record)

    repo, pr_number, branch = "ClawHaven/acme-app", 91, "agent/eng-20"
    work_item_id, _ = _park_work_item(repo=repo, pr_number=pr_number, branch=branch)

    await _fire_closed(
        repo=repo, pr_number=pr_number, branch=branch, tmp_path=tmp_path, merged=False
    )

    assert pushed == [(work_item_id, SemanticStatus.READY_FOR_AGENT)]


@pytest.mark.asyncio
async def test_external_merge_pushes_done(db_session, tmp_path, monkeypatch):
    """An externally-merged PR mirrors druks's own merge op: ticket → Done."""
    from druks.build.models import WorkItem
    from druks.ticketing.enums import SemanticStatus

    pushed = []

    async def _record(self, status):
        pushed.append((self.id, status))

    monkeypatch.setattr(WorkItem, "set_remote_status", _record)

    repo, pr_number, branch = "ClawHaven/acme-app", 92, "agent/eng-21"
    work_item_id, _ = _park_work_item(repo=repo, pr_number=pr_number, branch=branch)

    await _fire_closed(
        repo=repo, pr_number=pr_number, branch=branch, tmp_path=tmp_path, merged=True
    )

    assert pushed == [(work_item_id, SemanticStatus.DONE)]


@pytest.mark.asyncio
async def test_external_close_honors_delete_branch_policy(db_session, tmp_path, monkeypatch):
    """delete_branch: false in the item's extension-config snapshot keeps the
    head branch on an external close."""
    from druks.build import subscribers as webhooks_mod

    deleted = []

    async def _record(repo, branch):
        deleted.append((repo, branch))

    monkeypatch.setattr(webhooks_mod, "_delete_branch", _record)

    repo, pr_number, branch = "ClawHaven/acme-app", 93, "agent/eng-22"
    work_item_id, _ = _park_work_item(repo=repo, pr_number=pr_number, branch=branch)
    from druks.build.models import WorkItem

    WorkItem.get(work_item_id).update(
        extension_config_snapshot={"policy": {"delete_branch": False}}
    )

    await _fire_closed(
        repo=repo, pr_number=pr_number, branch=branch, tmp_path=tmp_path, merged=False
    )

    assert deleted == []


@pytest.mark.asyncio
async def test_external_close_deletes_branch_by_default(db_session, tmp_path, monkeypatch):
    from druks.build import subscribers as webhooks_mod

    deleted = []

    async def _record(repo, branch):
        deleted.append((repo, branch))

    monkeypatch.setattr(webhooks_mod, "_delete_branch", _record)

    repo, pr_number, branch = "ClawHaven/acme-app", 94, "agent/eng-23"
    _park_work_item(repo=repo, pr_number=pr_number, branch=branch)

    await _fire_closed(
        repo=repo, pr_number=pr_number, branch=branch, tmp_path=tmp_path, merged=False
    )

    assert deleted == [(repo, branch)]

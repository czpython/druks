from datetime import UTC, datetime
from types import SimpleNamespace

import druks.contrib.ship.subscribers  # noqa: F401 — registers the pr.closed subscriber
import pytest
from druks.contrib.ship.models import WorkItem
from druks.core.webhooks.github import GitHubEvents
from druks.durable import Run
from druks.events.models import Event
from druks.testing import make_settings
from sqlalchemy import func, select

from ship.factories import make_test_work_item, seed_build_run

# GitHub's own stamp on the verdict, which druks stores rather than its receipt time.
_RESOLVED_AT = "2026-07-25T21:59:09Z"


@pytest.fixture(autouse=True)
def _stub_config_fetch(monkeypatch):
    # The external-close path resolves the repo's live .druks/ship/config.yml;
    # default to "no file" (default policy) so tests don't reach GitHub.
    async def _fetch(*, repo, path):
        return None

    monkeypatch.setattr("druks.extensions.config.fetch_file", _fetch)


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


async def _fire_closed(*, repo, pr_number, branch, tmp_path, merged=True, at=_RESOLVED_AT):
    payload = {
        "repository": {"full_name": repo},
        "pull_request": {
            "number": pr_number,
            "merged": merged,
            "merged_at": at if merged else None,
            "closed_at": at,
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


def _park_work_item(*, repo, pr_number, branch, state="parked", input_gate="review_work"):
    """A work item with a build run paused on the operator (review_work) — the
    haunting case. Returns (work_item_id, build_run_id)."""
    from druks.database import db_session

    item = make_test_work_item(repo=repo, title="Externally merged")
    item.update(pr_number=pr_number, branch=branch)
    run = seed_build_run(
        db_session(),
        work_item_id=item.id,
        state=state,
        input_gate=input_gate if state == "parked" else None,
    )
    return item.id, run.id


def _fresh_run(run_id):
    # Workflow.cancel() never writes state — re-select before reading the derived one.
    from druks.database import db_session

    db_session().expire_all()
    return Run.get(run_id)


@pytest.mark.asyncio
async def test_external_merge_stores_githubs_verdict_and_ends_involvement(druks_db, tmp_path):
    repo, pr_number, branch = "ClawHaven/acme-app", 42, "agent/eng-1"
    work_item_id, run_id = _park_work_item(repo=repo, pr_number=pr_number, branch=branch)

    await _fire_closed(repo=repo, pr_number=pr_number, branch=branch, tmp_path=tmp_path)

    # The verdict is stored with GitHub's own stamp, not druks's receipt time...
    item = WorkItem.get(work_item_id)
    assert item.resolution == "merged"
    assert item.resolved_at == datetime(2026, 7, 25, 21, 59, 9, tzinfo=UTC)
    # ...announced as the milestone...
    assert _milestone_count(work_item_id, "merged") == 1
    # ...and involvement ended: the parked build run is cancelled.
    assert not _fresh_run(run_id).is_active


@pytest.mark.asyncio
async def test_merge_ships_but_leaves_a_running_build_to_converge(druks_db, tmp_path):
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
    assert _milestone_count(work_item_id, "merged") == 1


@pytest.mark.asyncio
async def test_a_redelivered_webhook_does_not_rewrite_the_verdict(druks_db, tmp_path):
    """GitHub redelivers webhooks, and druks's own merge echoes back through the
    same path. The stored verdict is the first one; a contradicting redelivery
    neither overwrites it nor records a second milestone."""
    repo, pr_number, branch = "ClawHaven/acme-app", 44, "agent/eng-3"
    work_item_id, _ = _park_work_item(repo=repo, pr_number=pr_number, branch=branch)
    await _fire_closed(repo=repo, pr_number=pr_number, branch=branch, tmp_path=tmp_path)

    await _fire_closed(
        repo=repo,
        pr_number=pr_number,
        branch=branch,
        tmp_path=tmp_path,
        merged=False,
        at="2026-07-26T13:30:30Z",
    )

    item = WorkItem.get(work_item_id)
    assert item.resolution == "merged"
    assert item.resolved_at == datetime(2026, 7, 25, 21, 59, 9, tzinfo=UTC)
    assert _milestone_count(work_item_id, "merged") == 1
    assert _milestone_count(work_item_id, "closed") == 0


@pytest.mark.asyncio
async def test_closed_unmerged_stores_closed_and_ends_involvement(druks_db, tmp_path):
    """A PR closed *without* merging — the operator abandoned it (e.g. deleted
    the branch). Store GitHub's 'closed' and un-park, so the item leaves the
    active board for History rather than being ignored."""
    repo, pr_number, branch = "ClawHaven/acme-app", 45, "agent/eng-4"
    work_item_id, run_id = _park_work_item(repo=repo, pr_number=pr_number, branch=branch)

    await _fire_closed(
        repo=repo,
        pr_number=pr_number,
        branch=branch,
        tmp_path=tmp_path,
        merged=False,
    )

    assert WorkItem.get(work_item_id).resolution == "closed"
    assert _milestone_count(work_item_id, "closed") == 1
    assert _milestone_count(work_item_id, "merged") == 0
    assert not _fresh_run(run_id).is_active


@pytest.mark.asyncio
async def test_closed_unmerged_cancels_in_flight_run(druks_db, tmp_path):
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
    assert _milestone_count(work_item_id, "closed") == 1


@pytest.mark.asyncio
async def test_a_merge_after_a_failed_build_still_settles_the_item(druks_db, tmp_path):
    """The production failure this fixes: the run failed and stays failed, so
    nothing in the run lifecycle can announce the operator's later manual merge.
    GitHub's does, and the stored verdict takes the item to History."""
    repo, pr_number, branch = "ClawHaven/acme-app", 126, "agent/eng-760"
    work_item_id, _ = _park_work_item(
        repo=repo,
        pr_number=pr_number,
        branch=branch,
        state="failed",
    )

    await _fire_closed(repo=repo, pr_number=pr_number, branch=branch, tmp_path=tmp_path)

    item = WorkItem.get(work_item_id)
    assert item.resolution == "merged"
    assert item.id not in {summary.id for summary in WorkItem.list_summaries()}
    assert [row.id for row in WorkItem.list_handoff()] == [item.id]


@pytest.mark.asyncio
async def test_a_remerge_after_redispatch_records_a_fresh_verdict(druks_db, tmp_path):
    """A new build takes the item over and drops the prior round's verdict, so
    the next merge is stored on its own terms instead of being read as a
    redelivery of the last one."""
    from druks.database import db_session as ds

    repo, pr_number, branch = "ClawHaven/acme-app", 77, "agent/eng-9"
    work_item_id, _ = _park_work_item(repo=repo, pr_number=pr_number, branch=branch)
    await _fire_closed(repo=repo, pr_number=pr_number, branch=branch, tmp_path=tmp_path)
    # Redispatched: a newer build run owns the item, and its PR is a fresh one.
    new_run = seed_build_run(ds(), work_item_id=work_item_id, state="running")
    WorkItem.get(work_item_id).update(
        build_run_id=new_run.id, branch=None, pr_number=None, resolution=None, resolved_at=None
    )
    WorkItem.get(work_item_id).update(pr_number=pr_number, branch=branch)

    await _fire_closed(repo=repo, pr_number=pr_number, branch=branch, tmp_path=tmp_path)

    assert WorkItem.get(work_item_id).resolution == "merged"
    assert _milestone_count(work_item_id, "merged") == 2


@pytest.mark.asyncio
async def test_external_close_returns_ticket_to_resting_pool(druks_db, tmp_path, monkeypatch):
    """Closing the PR abandons the attempt, not the ticket: druks pushes the
    provider's resting status (Linear → Backlog, Jira → Open) so the
    ticket doesn't strand in In Progress/Review."""
    from druks.contrib.ship.models import WorkItem
    from druks.ticketing.enums import TicketStatus

    pushed = []

    async def _record(self, status):
        pushed.append((self.id, status))

    monkeypatch.setattr(WorkItem, "set_ticket_status", _record)

    repo, pr_number, branch = "ClawHaven/acme-app", 91, "agent/eng-20"
    work_item_id, _ = _park_work_item(repo=repo, pr_number=pr_number, branch=branch)

    await _fire_closed(
        repo=repo, pr_number=pr_number, branch=branch, tmp_path=tmp_path, merged=False
    )

    assert pushed == [(work_item_id, TicketStatus.READY_FOR_AGENT)]


@pytest.mark.asyncio
async def test_external_merge_pushes_done(druks_db, tmp_path, monkeypatch):
    """An externally-merged PR mirrors druks's own merge op: ticket → Done."""
    from druks.contrib.ship.models import WorkItem
    from druks.ticketing.enums import TicketStatus

    pushed = []

    async def _record(self, status):
        pushed.append((self.id, status))

    monkeypatch.setattr(WorkItem, "set_ticket_status", _record)

    repo, pr_number, branch = "ClawHaven/acme-app", 92, "agent/eng-21"
    work_item_id, _ = _park_work_item(repo=repo, pr_number=pr_number, branch=branch)

    await _fire_closed(
        repo=repo, pr_number=pr_number, branch=branch, tmp_path=tmp_path, merged=True
    )

    assert pushed == [(work_item_id, TicketStatus.DONE)]


@pytest.mark.asyncio
async def test_external_close_honors_delete_branch_policy(druks_db, tmp_path, monkeypatch):
    """delete_branch: false in the repo's live .druks/ship/config.yml keeps the
    head branch on an external close."""
    from druks.contrib.ship import models as build_models

    async def _fetch(*, repo, path):
        return "delete_branch: false\n"

    monkeypatch.setattr("druks.extensions.config.fetch_file", _fetch)

    deleted = []

    async def _record(repo, branch):
        deleted.append((repo, branch))

    monkeypatch.setattr(
        build_models, "get_github_client", lambda settings: SimpleNamespace(delete_branch=_record)
    )

    repo, pr_number, branch = "ClawHaven/acme-app", 93, "agent/eng-22"
    _park_work_item(repo=repo, pr_number=pr_number, branch=branch)

    await _fire_closed(
        repo=repo, pr_number=pr_number, branch=branch, tmp_path=tmp_path, merged=False
    )

    assert deleted == []


@pytest.mark.asyncio
async def test_external_close_deletes_branch_by_default(druks_db, tmp_path, monkeypatch):
    from druks.contrib.ship import models as build_models

    deleted = []

    async def _record(repo, branch):
        deleted.append((repo, branch))

    monkeypatch.setattr(
        build_models, "get_github_client", lambda settings: SimpleNamespace(delete_branch=_record)
    )

    repo, pr_number, branch = "ClawHaven/acme-app", 94, "agent/eng-23"
    _park_work_item(repo=repo, pr_number=pr_number, branch=branch)

    await _fire_closed(
        repo=repo, pr_number=pr_number, branch=branch, tmp_path=tmp_path, merged=False
    )

    assert deleted == [(repo, branch)]


@pytest.mark.asyncio
async def test_external_close_survives_policy_resolution_failure(druks_db, tmp_path, monkeypatch):
    """Branch cleanup is best-effort: a policy-resolution failure must not strand
    the ticket — the cancel and resting-pool reset still happen."""
    from druks.contrib.ship import models as build_models
    from druks.contrib.ship.policy import RepoPolicy
    from druks.ticketing.enums import TicketStatus

    async def _boom(cls, repo):
        raise RuntimeError("github down")

    monkeypatch.setattr(RepoPolicy, "resolve", classmethod(_boom))

    deleted = []

    async def _delete(repo, branch):
        deleted.append((repo, branch))

    monkeypatch.setattr(
        build_models, "get_github_client", lambda settings: SimpleNamespace(delete_branch=_delete)
    )

    pushed = []

    async def _record(self, status):
        pushed.append(status)

    monkeypatch.setattr(WorkItem, "set_ticket_status", _record)

    repo, pr_number, branch = "ClawHaven/acme-app", 95, "agent/eng-24"
    work_item_id, _ = _park_work_item(repo=repo, pr_number=pr_number, branch=branch)

    await _fire_closed(
        repo=repo, pr_number=pr_number, branch=branch, tmp_path=tmp_path, merged=False
    )

    assert deleted == []  # cleanup skipped when policy can't be resolved
    assert pushed == [TicketStatus.READY_FOR_AGENT]  # ticket still reset
    assert WorkItem.get(work_item_id).resolution == "closed"


@pytest.mark.asyncio
async def test_stale_close_after_redispatch_spares_the_new_run(druks_db, tmp_path):
    """A delayed pr.closed for a superseded attempt's PR must not touch the new
    run: re-dispatch cleared the item's branch/PR, so the stale close no longer
    resolves the item."""
    from druks.database import db_session as ds

    repo, pr_a, branch_a = "ClawHaven/acme-app", 61, "agent/eng-old"
    item = make_test_work_item(repo=repo, title="Re-dispatched")
    item.update(pr_number=pr_a, branch=branch_a)
    # Re-dispatch: a new run takes over and the prior attempt's branch/PR clear.
    new_run = seed_build_run(ds(), work_item_id=item.id, state="running")
    item.update(
        build_run_id=new_run.id, branch=None, pr_number=None, resolution=None, resolved_at=None
    )

    await _fire_closed(repo=repo, pr_number=pr_a, branch=branch_a, tmp_path=tmp_path, merged=False)

    assert _fresh_run(new_run.id).state == "running"  # the live attempt is untouched
    assert item.resolution is None

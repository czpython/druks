from datetime import datetime
from types import SimpleNamespace

import druks.contrib.ship.subscribers  # noqa: F401 — registers the pr.closed subscriber
import pytest
from druks.contrib.ship.models import WorkItem
from druks.core.webhooks.github import GitHubEvents
from druks.durable import Run
from druks.events.models import Event
from druks.testing import configure_app_for_test, make_settings
from fastapi.testclient import TestClient

from ship.factories import make_test_work_item, seed_build_run

_MERGED_AT = "2026-07-25T21:59:09Z"
_CLOSED_AT = "2026-07-26T13:30:30Z"


@pytest.fixture(autouse=True)
def _stub_config_fetch(monkeypatch):
    # The external-close path resolves the repo's live .druks/ship/config.yml;
    # default to "no file" (default policy) so tests don't reach GitHub.
    async def _fetch(*, repo, path):
        return None

    monkeypatch.setattr("druks.extensions.config.fetch_file", _fetch)


async def _fire_closed(
    *,
    repo,
    pr_number,
    branch,
    tmp_path,
    merged=True,
    merged_at=_MERGED_AT,
    closed_at=_CLOSED_AT,
):
    payload = {
        "repository": {"full_name": repo},
        "pull_request": {
            "number": pr_number,
            "merged": merged,
            "merged_at": merged_at,
            "closed_at": closed_at,
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
async def test_external_merge_stores_resolution_and_ends_involvement(druks_db, tmp_path):
    repo, pr_number, branch = "ClawHaven/acme-app", 42, "agent/eng-1"
    work_item_id, run_id = _park_work_item(repo=repo, pr_number=pr_number, branch=branch)

    await _fire_closed(repo=repo, pr_number=pr_number, branch=branch, tmp_path=tmp_path)

    resolved = WorkItem.get(work_item_id)
    assert resolved.pr_merged is True
    assert resolved.pr_resolved_at == datetime.fromisoformat(
        _MERGED_AT.replace("Z", "+00:00")
    )
    events = druks_db.query(Event).filter_by(subject_id=str(work_item_id)).all()
    assert "shipped" in {event.type for event in events}
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
    assert WorkItem.get(work_item_id).pr_merged is True


@pytest.mark.asyncio
async def test_redelivered_webhook_does_not_rewrite_resolution(druks_db, tmp_path):
    repo, pr_number, branch = "ClawHaven/acme-app", 44, "agent/eng-3"
    work_item_id, _ = _park_work_item(repo=repo, pr_number=pr_number, branch=branch)
    await _fire_closed(repo=repo, pr_number=pr_number, branch=branch, tmp_path=tmp_path)
    resolved_at = WorkItem.get(work_item_id).pr_resolved_at

    await _fire_closed(
        repo=repo,
        pr_number=pr_number,
        branch=branch,
        tmp_path=tmp_path,
        merged=False,
    )

    item = WorkItem.get(work_item_id)
    assert item.pr_merged is True
    assert item.pr_resolved_at == resolved_at


@pytest.mark.asyncio
async def test_failed_build_later_merged_moves_from_board_to_history(druks_db, tmp_path):
    repo, pr_number, branch = "ClawHaven/acme-app", 126, "agent/eng-760"
    work_item_id, _ = _park_work_item(
        repo=repo,
        pr_number=pr_number,
        branch=branch,
        state="failed",
    )

    await _fire_closed(repo=repo, pr_number=pr_number, branch=branch, tmp_path=tmp_path)

    item = WorkItem.get(work_item_id)
    assert item.pr_merged is True
    settings = make_settings(tmp_path)
    with TestClient(configure_app_for_test(settings=settings)) as client:
        active = client.get("/api/ship/work_item").json()["rows"]
        history = client.get("/api/ship/work-items/history").json()["items"]

    assert str(work_item_id) not in {row["summary"]["id"] for row in active}
    resolved = next(row for row in history if row["sourceId"] == work_item_id)
    assert resolved["resolution"] == "merged"
    resolved_at = datetime.fromisoformat(resolved["updatedAt"].replace("Z", "+00:00"))
    assert resolved_at == item.pr_resolved_at


@pytest.mark.asyncio
async def test_closed_unmerged_records_close_and_ends_involvement(druks_db, tmp_path):
    """A PR closed without merging stores GitHub's outcome and cancels the run."""
    repo, pr_number, branch = "ClawHaven/acme-app", 45, "agent/eng-4"
    work_item_id, run_id = _park_work_item(repo=repo, pr_number=pr_number, branch=branch)

    await _fire_closed(
        repo=repo,
        pr_number=pr_number,
        branch=branch,
        tmp_path=tmp_path,
        merged=False,
    )

    resolved = WorkItem.get(work_item_id)
    assert resolved.pr_merged is False
    assert resolved.pr_resolved_at == datetime.fromisoformat(
        _CLOSED_AT.replace("Z", "+00:00")
    )
    events = druks_db.query(Event).filter_by(subject_id=str(work_item_id)).all()
    assert "cancelled" in {event.type for event in events}
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
    assert WorkItem.get(work_item_id).pr_merged is False


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
    assert WorkItem.get(work_item_id).pr_merged is False


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
    item.update(branch=None, pr_number=None)

    await _fire_closed(repo=repo, pr_number=pr_a, branch=branch_a, tmp_path=tmp_path, merged=False)

    assert _fresh_run(new_run.id).state == "running"  # the live attempt is untouched
    assert item.pr_merged is None
    assert item.pr_resolved_at is None

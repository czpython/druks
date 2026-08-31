from datetime import UTC, datetime
from pathlib import Path

import pytest
from druks.contrib.software_factory.models import WorkItem
from druks.durable.reads import list_subject_timeline
from druks.testing import asgi_client, seed_call
from fastapi.testclient import TestClient

from software_factory.factories import make_test_work_item, seed_build_run

_RUN_STATE = {
    "running": "running",
    "finished": "finished",
    "failed": "failed",
    "cancelled": "cancelled",
}


def _build_app(tmp_path):
    from druks.testing import configure_app_for_test, make_settings

    settings = make_settings(tmp_path)
    return configure_app_for_test(settings=settings)


@pytest.fixture
async def client(tmp_path: Path, druks_db):
    async with asgi_client(_build_app(tmp_path)) as client:
        yield client


_GATE_REQUESTS = {
    "review_plan": {"next_action": "approve_plan", "label": "Approve plan"},
    "answer_questions": {"next_action": "answer_questions", "label": "Answer questions"},
    "review_work": {"next_action": "review_work", "label": "Review implementation"},
}


async def _seed_op(druks_db, work_item_id, *, kind="implement", state, input_gate=None):
    """A build run on the item in ``state`` whose latest agent call is ``kind``.
    When a run already exists, advance it (re-trigger = a fresh round on the same
    item)."""
    if state == "running" and input_gate:
        run = await seed_build_run(
            druks_db,
            work_item_id=work_item_id,
            state="parked",
            input_gate=input_gate,
            input_request=_GATE_REQUESTS.get(input_gate),
        )
    else:
        run = await seed_build_run(druks_db, work_item_id=work_item_id, state=_RUN_STATE[state])
    await seed_call(druks_db, run, kind)


async def _resolve(repo, pr_number, *, merged=True):
    """GitHub's verdict on a work item's PR — what lands it in History, as the
    merge handler stores it."""
    item = await WorkItem.get_for_pr(repo=repo, pr_number=pr_number)
    if item:
        await item.resolve(merged=merged, at=datetime.now(UTC))


# The generic subject read-side — Build declares subject = WorkItem, so the
# platform mounts /api/software_factory/work_item (list) and /{id} (detail). WorkItem supplies
# only the domain summary; status (RunState-aggregated) and the timeline are the
# platform's. See test_generic_subjects.py for the platform-side contract.


async def test_subject_list_shows_active_and_excludes_resolved(client: TestClient, druks_db):
    repo = "ClawHaven/acme-app"
    building = (await make_test_work_item(title="building", repo=repo)).id
    await _seed_op(druks_db, building, state="running")
    # Merged → History, not the active board.
    done = (await make_test_work_item(title="merged one", repo=repo)).id
    await (await WorkItem.get(done)).update(pr_number=1)
    await _seed_op(druks_db, done, state="finished")
    await _resolve(repo, 1)

    rows = {
        r["summary"]["title"]: r
        for r in (await client.get("/api/software_factory/work_item")).json()["rows"]
    }
    assert "building" in rows
    assert "merged one" not in rows
    assert rows["building"]["status"]["state"] == "running"
    assert rows["building"]["summary"]["resolution"] is None


async def test_subject_detail_composes_summary_status_and_timeline(client: TestClient, druks_db):
    item = await make_test_work_item(
        title="detail",
        repo="ClawHaven/acme-app",
        source="linear",
        ticket_key="ACME-5",
        ticket_url="https://linear.app/acme/issue/ACME-5/detail",
    )
    await (await WorkItem.get(item.id)).update(pr_number=8)
    run = await seed_build_run(
        druks_db,
        work_item_id=item.id,
        state="parked",
        input_gate="review_plan",
        input_request={"next_action": "approve_plan", "label": "Approve plan"},
    )
    await seed_call(druks_db, run, "generate_plan")

    detail = (await client.get(f"/api/software_factory/work_item/{item.id}")).json()
    summary = detail["summary"]
    assert summary["id"] == str(item.id)
    assert summary["ticketKey"] == "ACME-5"
    assert summary["links"]["ticket"] == "https://linear.app/acme/issue/ACME-5/detail"
    assert summary["links"]["pr"] == "https://github.com/ClawHaven/acme-app/pull/8"
    # Status is the platform's, aggregated from the item's runs — parked on a gate.
    assert detail["status"]["state"] == "parked"
    assert detail["status"]["gate"] == "review_plan"
    # The timeline is the platform's: the run itself, carrying its gate ask and
    # its agent calls.
    (entry,) = detail["timeline"]
    assert entry["id"] == run.id
    assert entry["state"] == "parked"
    assert entry["inputRequest"] == {"next_action": "approve_plan", "label": "Approve plan"}
    assert [call["agent"] for call in entry["agentCalls"]] == ["generate_plan"]


async def test_subject_detail_unknown_is_404(client: TestClient):
    assert (await client.get("/api/software_factory/work_item/9999")).status_code == 404


async def test_pending_gate_surfaces_input_request_on_the_run(druks_db):
    # A gate is run-level: the parked run carries its own ask on the timeline,
    # with its agent calls in execution order underneath.
    item = await make_test_work_item(repo="ClawHaven/acme-app", title="x")
    run = await seed_build_run(
        druks_db,
        work_item_id=item.id,
        state="parked",
        input_gate="review_plan",
        input_request={"next_action": "approve_plan", "label": "Approve plan"},
    )
    await seed_call(druks_db, run, "generate_plan")
    await seed_call(druks_db, run, "review_plan")

    (entry,) = await list_subject_timeline(item.subject_type, str(item.id))
    assert entry.input_request == {"next_action": "approve_plan", "label": "Approve plan"}
    assert entry.state == "parked"
    assert [call.agent for call in entry.agent_calls] == ["generate_plan", "review_plan"]


async def test_detail_surfaces_running_run_before_its_first_call(druks_db):
    """The detail timeline surfaces a run that is running before its first agent
    call exists — the sandbox spin-up window the operator needs to see."""
    item = await make_test_work_item(repo="ClawHaven/acme-app", title="x")
    await seed_build_run(druks_db, work_item_id=item.id, state="running")

    (entry,) = await list_subject_timeline(item.subject_type, str(item.id))
    assert entry.state == "running"
    assert entry.agent_calls == []  # surfaces even with no call yet


async def test_history_returns_only_done_work_items(client: TestClient, druks_db):
    repo = "ClawHaven/acme-app"
    # Merged → history.
    done_id = (await make_test_work_item(title="merged one", repo=repo)).id
    await (await WorkItem.get(done_id)).update(pr_number=1)
    await _seed_op(druks_db, done_id, state="finished")
    await _resolve(repo, 1)
    # Running → active.
    running_id = (await make_test_work_item(title="still running", repo=repo)).id
    await _seed_op(druks_db, running_id, state="running")
    # Failed (no merge) → active "needs you", NOT history (the whole point).
    failed_id = (await make_test_work_item(title="broke", repo=repo)).id
    await (await WorkItem.get(failed_id)).update(pr_number=2)
    await _seed_op(druks_db, failed_id, state="failed")

    items = (await client.get("/api/software_factory/work-items/history")).json()["items"]
    titles = [it["title"] for it in items]
    assert "merged one" in titles
    assert "still running" not in titles
    assert "broke" not in titles  # failed items stay active, not history
    resolved = next(it for it in items if it["title"] == "merged one")
    assert resolved["resolution"] == "merged"


async def test_pr_closed_without_merge_is_closed_in_history(client: TestClient, druks_db):
    repo = "ClawHaven/acme-app"
    # A build parked on the operator, whose PR was then closed without merging.
    wid = (await make_test_work_item(title="abandoned", repo=repo)).id
    await (await WorkItem.get(wid)).update(pr_number=7)
    await _seed_op(druks_db, wid, state="finished")
    await _resolve(repo, 7, merged=False)

    items = (await client.get("/api/software_factory/work-items/history")).json()["items"]
    row = next(it for it in items if it["title"] == "abandoned")
    assert row["resolution"] == "closed"
    # History's time column is the verdict's, not the row's last touch.
    assert datetime.fromisoformat(row["updatedAt"]) == (await WorkItem.get(wid)).resolved_at


async def test_history_clamps_limit(client: TestClient, druks_db):
    for i in range(3):
        wid = (await make_test_work_item(title=f"merged {i}", repo="ClawHaven/acme-app")).id
        await (await WorkItem.get(wid)).update(pr_number=i + 1)
        await _seed_op(druks_db, wid, state="finished")
        await _resolve("ClawHaven/acme-app", i + 1)

    # limit > cap → clamps down, doesn't 400.
    response = await client.get("/api/software_factory/work-items/history?limit=10000")
    assert response.status_code == 200
    items = response.json()["items"]
    assert len(items) == 3  # all three merged; cap doesn't truncate here

    # limit < 1 → clamps up to 1.
    response = await client.get("/api/software_factory/work-items/history?limit=0")
    assert response.status_code == 200
    items = response.json()["items"]
    assert len(items) == 1


async def test_repeated_runs_on_one_subject_each_surface_separately(druks_db):
    # The timeline must not collapse repeated runs to only the newest one.
    item = await make_test_work_item(repo="ClawHaven/acme-app", title="repeated")
    for _ in range(3):
        await seed_build_run(druks_db, work_item_id=item.id, state="finished")

    entries = await list_subject_timeline(item.subject_type, str(item.id))
    assert [entry.kind for entry in entries] == ["software_factory.build"] * 3
    assert len({entry.id for entry in entries}) == 3


async def test_timeline_shows_every_build_attempt(druks_db):
    # Each build attempt is its own run; the timeline shows them all, with a
    # failed attempt's failure carried on its run.
    item = await make_test_work_item(repo="ClawHaven/acme-app", title="x", ticket_key="ACME-1")
    run1 = await seed_build_run(druks_db, work_item_id=item.id, state="failed", failure="boom")
    run2 = await seed_build_run(druks_db, work_item_id=item.id, state="failed")
    await seed_call(druks_db, run1, "generate_plan", status="failed", last_error="boom")
    await seed_call(druks_db, run2, "generate_plan", status="failed")

    entries = await list_subject_timeline(item.subject_type, str(item.id))
    assert len(entries) == 2
    assert all(e.agent_calls[0].agent == "generate_plan" for e in entries)
    assert any(e.failure == "boom" for e in entries)


async def test_subject_activity_surfaces_running_phase(druks_db, monkeypatch):
    # A running build run pushes a transient phase; the detail view's live activity
    # surfaces it ("Provisioning sandbox VM…") — finer than the lifecycle status.
    from druks.contrib.software_factory import app as software_factory_app

    item = await make_test_work_item(repo="ClawHaven/acme-app", title="x")
    await seed_build_run(druks_db, work_item_id=item.id, state="running")

    async def phase(_run_id):
        return "provisioning_vm"

    monkeypatch.setattr("druks.durable.reads.get_run_phase", phase)
    activity = await software_factory_app.SoftwareFactory.get_subject_activity(item)
    assert activity is not None
    assert activity.label == "Provisioning sandbox VM…"
    assert activity.kind == "infra"


async def test_subject_activity_none_when_not_running(druks_db):
    # A run parked on a gate isn't working — no live sub-phase.
    from druks.contrib.software_factory import app as software_factory_app

    item = await make_test_work_item(repo="ClawHaven/acme-app", title="x")
    await seed_build_run(druks_db, work_item_id=item.id, state="parked", input_gate="review_plan")

    assert await software_factory_app.SoftwareFactory.get_subject_activity(item) is None

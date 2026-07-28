from pathlib import Path

import pytest
from druks.contrib.ship.models import WorkItem
from druks.testing import seed_call
from fastapi.testclient import TestClient

from ship.factories import make_test_work_item, seed_build_run

_RUN_STATE = {
    "running": "running",
    "finished": "finished",
    "failed": "failed",
    "cancelled": "cancelled",
}


def _build_client(tmp_path):
    from druks.testing import configure_app_for_test, make_settings

    settings = make_settings(tmp_path)
    app = configure_app_for_test(settings=settings)
    return TestClient(app)


@pytest.fixture
def client(tmp_path: Path, druks_db):
    with _build_client(tmp_path) as client:
        yield client


_GATE_REQUESTS = {
    "review_plan": {"next_action": "approve_plan", "label": "Approve plan"},
    "answer_questions": {"next_action": "answer_questions", "label": "Answer questions"},
    "review_work": {"next_action": "review_work", "label": "Review implementation"},
}


def _seed_op(druks_db, work_item_id, *, kind="implement", state, input_gate=None):
    """A build run on the item in ``state`` whose latest agent call is ``kind``.
    When a run already exists, advance it (re-trigger = a fresh round on the same
    item), rebinding ``build_run_id`` to the newest run."""
    if state == "running" and input_gate:
        run = seed_build_run(
            druks_db,
            work_item_id=work_item_id,
            state="parked",
            input_gate=input_gate,
            input_request=_GATE_REQUESTS.get(input_gate),
        )
    else:
        run = seed_build_run(druks_db, work_item_id=work_item_id, state=_RUN_STATE[state])
    seed_call(druks_db, run, kind)


def _ship(repo, pr_number):
    """Merge a work item's PR — the 'shipped' log event that lands it in
    History, mirroring the merge handler."""
    item = WorkItem.get_for_pr(repo=repo, pr_number=pr_number)
    if item:
        item.set_status("shipped")


# The generic subject read-side — Build declares subject = WorkItem, so the
# platform mounts /api/ship/work_item (list) and /{id} (detail). WorkItem supplies
# only the domain summary; status (RunState-aggregated) and the timeline are the
# platform's. See test_generic_subjects.py for the platform-side contract.


def test_subject_list_shows_active_and_excludes_handed_off(client: TestClient, druks_db):
    repo = "ClawHaven/acme-app"
    building = make_test_work_item(title="building", repo=repo).id
    _seed_op(druks_db, building, state="running")
    # Shipped → terminal handoff → History, not the active board.
    done = make_test_work_item(title="shipped one", repo=repo).id
    WorkItem.get(done).update(pr_number=1)
    _seed_op(druks_db, done, state="finished")
    _ship(repo, 1)

    rows = {r["summary"]["title"]: r for r in client.get("/api/ship/work_item").json()["rows"]}
    assert "building" in rows
    assert "shipped one" not in rows
    assert rows["building"]["status"]["state"] == "running"


def test_subject_detail_composes_summary_status_and_timeline(client: TestClient, druks_db):
    item = make_test_work_item(
        title="detail",
        repo="ClawHaven/acme-app",
        source="linear",
        ticket_key="ACME-5",
        ticket_url="https://linear.app/acme/issue/ACME-5/detail",
    )
    WorkItem.get(item.id).update(pr_number=8)
    run = seed_build_run(
        druks_db,
        work_item_id=item.id,
        state="parked",
        input_gate="review_plan",
        input_request={"next_action": "approve_plan", "label": "Approve plan"},
    )
    seed_call(druks_db, run, "generate_plan")

    detail = client.get(f"/api/ship/work_item/{item.id}").json()
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


def test_subject_detail_unknown_is_404(client: TestClient):
    assert client.get("/api/ship/work_item/9999").status_code == 404


def test_pending_gate_surfaces_input_request_on_the_run(druks_db):
    # A gate is run-level: the parked run carries its own ask on the timeline,
    # with its agent calls in execution order underneath.
    item = make_test_work_item(repo="ClawHaven/acme-app", title="x")
    run = seed_build_run(
        druks_db,
        work_item_id=item.id,
        state="parked",
        input_gate="review_plan",
        input_request={"next_action": "approve_plan", "label": "Approve plan"},
    )
    seed_call(druks_db, run, "generate_plan")
    seed_call(druks_db, run, "review_plan")

    (entry,) = item.get_timeline()
    assert entry.input_request == {"next_action": "approve_plan", "label": "Approve plan"}
    assert entry.state == "parked"
    assert [call.agent for call in entry.agent_calls] == ["generate_plan", "review_plan"]


def test_detail_surfaces_running_run_before_its_first_call(druks_db):
    """The detail timeline surfaces a run that is running before its first agent
    call exists — the sandbox spin-up window the operator needs to see."""
    item = make_test_work_item(repo="ClawHaven/acme-app", title="x")
    seed_build_run(druks_db, work_item_id=item.id, state="running")

    (entry,) = item.get_timeline()
    assert entry.state == "running"
    assert entry.agent_calls == []  # surfaces even with no call yet


def test_history_returns_only_done_work_items(client: TestClient, druks_db):
    repo = "ClawHaven/acme-app"
    # Shipped → history.
    done_id = make_test_work_item(title="shipped one", repo=repo).id
    WorkItem.get(done_id).update(pr_number=1)
    _seed_op(druks_db, done_id, state="finished")
    _ship(repo, 1)
    # Running → active.
    running_id = make_test_work_item(title="still running", repo=repo).id
    _seed_op(druks_db, running_id, state="running")
    # Failed (no merge) → active "needs you", NOT history (the whole point).
    failed_id = make_test_work_item(title="broke", repo=repo).id
    WorkItem.get(failed_id).update(pr_number=2)
    _seed_op(druks_db, failed_id, state="failed")

    items = client.get("/api/ship/work-items/history").json()["items"]
    titles = [it["title"] for it in items]
    assert "shipped one" in titles
    assert "still running" not in titles
    assert "broke" not in titles  # failed items stay active, not history


def test_pr_closed_without_merge_is_cancelled_in_history(client: TestClient, druks_db):
    repo = "ClawHaven/acme-app"
    # A build parked on the operator, whose PR was then closed without merging.
    wid = make_test_work_item(title="abandoned", repo=repo).id
    WorkItem.get(wid).update(pr_number=7)
    _seed_op(druks_db, wid, state="finished")
    WorkItem.get(wid).set_status("cancelled")

    items = client.get("/api/ship/work-items/history").json()["items"]
    row = next(it for it in items if it["title"] == "abandoned")
    assert row["status"] == "cancelled"


def test_history_clamps_limit(client: TestClient, druks_db):
    for i in range(3):
        wid = make_test_work_item(title=f"shipped {i}", repo="ClawHaven/acme-app").id
        WorkItem.get(wid).update(pr_number=i + 1)
        _seed_op(druks_db, wid, state="finished")
        _ship("ClawHaven/acme-app", i + 1)

    # limit > cap → clamps down, doesn't 400.
    response = client.get("/api/ship/work-items/history?limit=10000")
    assert response.status_code == 200
    items = response.json()["items"]
    assert len(items) == 3  # all three shipped; cap doesn't truncate here

    # limit < 1 → clamps up to 1.
    response = client.get("/api/ship/work-items/history?limit=0")
    assert response.status_code == 200
    items = response.json()["items"]
    assert len(items) == 1


def test_repeated_runs_on_one_subject_each_surface_separately(druks_db):
    # The timeline must not collapse repeated runs to only the newest one.
    item = make_test_work_item(repo="ClawHaven/acme-app", title="repeated")
    for _ in range(3):
        seed_build_run(druks_db, work_item_id=item.id, state="finished")

    entries = item.get_timeline()
    assert [entry.kind for entry in entries] == ["ship.build"] * 3
    assert len({entry.id for entry in entries}) == 3


def test_update_stamps_build_run_id(druks_db):
    # build intake stamps the owning run via update(build_run_id=...); the kwarg
    # was missing, so every "Ready for Agent" transition threw a TypeError.
    from druks.contrib.ship.models import WorkItem

    item = make_test_work_item(repo="ClawHaven/acme-app", title="x")
    run = seed_build_run(druks_db, work_item_id=item.id, state="running")
    item.update(build_run_id=run.id)
    assert WorkItem.get(item.id).build_run_id == run.id


def test_timeline_shows_every_build_attempt(druks_db):
    # Each build attempt is its own run; the timeline shows them all, with a
    # failed attempt's failure carried on its run.
    item = make_test_work_item(repo="ClawHaven/acme-app", title="x", ticket_key="ACME-1")
    run1 = seed_build_run(druks_db, work_item_id=item.id, state="failed", failure="boom")
    run2 = seed_build_run(druks_db, work_item_id=item.id, state="failed")
    seed_call(druks_db, run1, "generate_plan", status="failed", last_error="boom")
    seed_call(druks_db, run2, "generate_plan", status="failed")

    entries = item.get_timeline()
    assert len(entries) == 2
    assert all(e.agent_calls[0].agent == "generate_plan" for e in entries)
    assert any(e.failure == "boom" for e in entries)


async def test_subject_activity_surfaces_running_phase(druks_db, monkeypatch):
    # A running build run pushes a transient phase; the detail view's live activity
    # surfaces it ("Building sandbox VM…") — finer than the lifecycle status.
    from druks.contrib.ship import extension as ship_extension

    item = make_test_work_item(repo="ClawHaven/acme-app", title="x")
    seed_build_run(druks_db, work_item_id=item.id, state="running")

    async def phase(_run_id):
        return "provisioning_vm"

    monkeypatch.setattr("druks.durable.reads.get_run_phase", phase)
    activity = await ship_extension.Ship.get_subject_activity(item)
    assert activity is not None
    assert activity.label == "Building sandbox VM…"
    assert activity.kind == "infra"


async def test_subject_activity_none_when_not_running(druks_db):
    # A run parked on a gate isn't working — no live sub-phase.
    from druks.contrib.ship import extension as ship_extension

    item = make_test_work_item(repo="ClawHaven/acme-app", title="x")
    seed_build_run(druks_db, work_item_id=item.id, state="parked", input_gate="review_plan")

    assert await ship_extension.Ship.get_subject_activity(item) is None

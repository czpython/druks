from pathlib import Path

import pytest
from conftest import make_test_work_item, seed_build_run, seed_call, seed_dbos_status
from druks.build.models import WorkItem
from druks.durable import Run
from fastapi.testclient import TestClient
from uuid_utils import uuid7

_RUN_STATE = {
    "running": "running",
    "finished": "finished",
    "failed": "failed",
    "cancelled": "cancelled",
}


def _seed_scope_run(db_session, item, *, state="finished", status=None):
    """A scope run for ``item`` (keyed by its remote_key) with its ``scope``
    agent call. A needs_answers / split_recommended status parks the run on the
    ScopeReply gate (PENDING_INPUT + input_request) — seeded from the workflow's
    own gate ask so the board reads exactly what the workflow stores at the park
    point."""
    from druks.build.scoping.workflows import _PARKED_ASK, ScopeReply

    parked = _PARKED_ASK.get(status) if status else None
    run = Run(
        id=str(uuid7()),
        kind="build.scope",
        input_gate=ScopeReply.topic if parked else None,
        input_request=parked.model_dump(mode="json") if parked else None,
    )
    db_session.add(run)
    db_session.flush()
    seed_dbos_status(
        db_session,
        run.id,
        "pending_input" if parked else state,
        subject={"type": "work_item", "id": item.id},
    )
    seed_call(db_session, run, "scope", status="failed" if state == "failed" else "succeeded")
    handoff = {"ready": "scoped"}.get(status)
    if handoff is not None:
        item.set_status(handoff)
    elif status is not None:
        from druks.events.models import Event

        Event.emit(type=status, subject=WorkItem.subject_for(item.id))
        db_session.flush()
    return run


def _build_client(tmp_path):
    from conftest import configure_app_for_test, make_settings

    settings = make_settings(tmp_path)
    app = configure_app_for_test(settings=settings)
    return TestClient(app)


@pytest.fixture
def client(tmp_path: Path, db_session):
    with _build_client(tmp_path) as client:
        yield client


_GATE_REQUESTS = {
    "review_plan": {"next_action": "approve_plan", "label": "Approve plan"},
    "answer_questions": {"next_action": "answer_questions", "label": "Answer questions"},
    "review_work": {"next_action": "review_work", "label": "Review implementation"},
}


def _seed_op(db_session, work_item_id, *, kind="implement", state, input_gate=None):
    """A build run on the item in ``state`` whose latest agent call is ``kind``.
    When a run already exists, advance it (re-trigger = a fresh round on the same
    item), rebinding ``build_run_id`` to the newest run."""
    if state == "running" and input_gate:
        run = seed_build_run(
            db_session,
            work_item_id=work_item_id,
            state="pending_input",
            input_gate=input_gate,
            input_request=_GATE_REQUESTS.get(input_gate),
        )
    else:
        run = seed_build_run(db_session, work_item_id=work_item_id, state=_RUN_STATE[state])
    seed_call(db_session, run, kind)


def _ship(repo, pr_number):
    """Merge a work item's PR — the 'shipped' log event that lands it in
    History, mirroring the merge handler."""
    item = WorkItem.get_for_pr(repo=repo, pr_number=pr_number)
    if item:
        item.set_status("shipped")


# The generic subject read-side — Build declares subject_type="work_item", so the
# platform mounts /api/build/work_item (list) and /{id} (detail). Build supplies
# only the domain summary; status (RunState-aggregated) and the timeline are the
# platform's. See test_generic_subjects.py for the platform-side contract.


def test_subject_list_shows_active_and_excludes_handed_off(client: TestClient, db_session):
    repo = "ClawHaven/acme-app"
    building = make_test_work_item(title="building", repo=repo).id
    _seed_op(db_session, building, state="running")
    # Shipped → terminal handoff → History, not the active board.
    done = make_test_work_item(title="shipped one", repo=repo).id
    WorkItem.get(done).update(pr_number=1)
    _seed_op(db_session, done, state="finished")
    _ship(repo, 1)

    rows = {r["summary"]["title"]: r for r in client.get("/api/build/work_item").json()["rows"]}
    assert "building" in rows
    assert "shipped one" not in rows
    assert rows["building"]["status"]["state"] == "running"


def test_subject_detail_composes_summary_status_and_timeline(client: TestClient, db_session):
    item = make_test_work_item(
        title="detail", repo="ClawHaven/acme-app", source="linear", remote_key="ACME-5"
    )
    WorkItem.get(item.id).update(pr_number=8)
    run = seed_build_run(
        db_session,
        work_item_id=item.id,
        state="pending_input",
        input_gate="review_plan",
        input_request={"next_action": "approve_plan", "label": "Approve plan"},
    )
    seed_call(db_session, run, "generate_plan")

    detail = client.get(f"/api/build/work_item/{item.id}").json()
    assert detail["summary"]["id"] == str(item.id)
    assert detail["summary"]["remoteKey"] == "ACME-5"
    assert detail["summary"]["links"]["pr"] == "https://github.com/ClawHaven/acme-app/pull/8"
    # Status is the platform's, aggregated from the item's runs — parked on a gate.
    assert detail["status"]["state"] == "pending_input"
    assert detail["status"]["gate"] == "review_plan"
    # The timeline is the platform's: the run itself, carrying its gate ask and
    # its agent calls.
    (entry,) = detail["timeline"]
    assert entry["id"] == run.id
    assert entry["state"] == "pending_input"
    assert entry["inputRequest"] == {"next_action": "approve_plan", "label": "Approve plan"}
    assert [call["agent"] for call in entry["agentCalls"]] == ["generate_plan"]


def test_subject_detail_unknown_is_404(client: TestClient):
    assert client.get("/api/build/work_item/9999").status_code == 404


def test_pending_gate_surfaces_input_request_on_the_run(db_session):
    # A gate is run-level: the parked run carries its own ask on the timeline,
    # with its agent calls in execution order underneath.
    from druks.durable.reads import list_subject_timeline

    item = make_test_work_item(repo="ClawHaven/acme-app", title="x")
    run = seed_build_run(
        db_session,
        work_item_id=item.id,
        state="pending_input",
        input_gate="review_plan",
        input_request={"next_action": "approve_plan", "label": "Approve plan"},
    )
    seed_call(db_session, run, "generate_plan")
    seed_call(db_session, run, "review_plan")

    (entry,) = list_subject_timeline("work_item", str(item.id))
    assert entry.input_request == {"next_action": "approve_plan", "label": "Approve plan"}
    assert entry.state == "pending_input"
    assert [call.agent for call in entry.agent_calls] == ["generate_plan", "review_plan"]


def test_detail_surfaces_running_run_before_its_first_call(db_session):
    """The detail timeline surfaces a run that is running before its first agent
    call exists — the sandbox spin-up window the operator needs to see."""
    from druks.durable.reads import list_subject_timeline

    work_item_id = make_test_work_item(repo="ClawHaven/acme-app", title="x").id
    seed_build_run(db_session, work_item_id=work_item_id, state="running")

    (entry,) = list_subject_timeline("work_item", str(work_item_id))
    assert entry.state == "running"
    assert entry.agent_calls == []  # surfaces even with no call yet


def test_history_returns_only_done_work_items(client: TestClient, db_session):
    repo = "ClawHaven/acme-app"
    # Shipped → history.
    done_id = make_test_work_item(title="shipped one", repo=repo).id
    WorkItem.get(done_id).update(pr_number=1)
    _seed_op(db_session, done_id, state="finished")
    _ship(repo, 1)
    # Running → active.
    running_id = make_test_work_item(title="still running", repo=repo).id
    _seed_op(db_session, running_id, state="running")
    # Failed (no merge) → active "needs you", NOT history (the whole point).
    failed_id = make_test_work_item(title="broke", repo=repo).id
    WorkItem.get(failed_id).update(pr_number=2)
    _seed_op(db_session, failed_id, state="failed")

    items = client.get("/api/build/work-items/history").json()["items"]
    titles = [it["title"] for it in items]
    assert "shipped one" in titles
    assert "still running" not in titles
    assert "broke" not in titles  # failed items stay active, not history


def test_scoped_item_lands_in_history(client: TestClient, db_session):
    item = make_test_work_item(
        title="scoped at rest", repo="ClawHaven/acme-app", source="linear", remote_key="ACME-50"
    )
    _seed_scope_run(db_session, item, status="ready")

    items = client.get("/api/build/work-items/history").json()["items"]
    row = next(it for it in items if it["title"] == "scoped at rest")
    assert row["outcome"] == "scoped"


def test_pr_closed_without_merge_is_cancelled_in_history(client: TestClient, db_session):
    repo = "ClawHaven/acme-app"
    # A build parked on the operator, whose PR was then closed without merging.
    wid = make_test_work_item(title="abandoned", repo=repo).id
    WorkItem.get(wid).update(pr_number=7)
    _seed_op(db_session, wid, state="finished")
    WorkItem.get(wid).set_status("cancelled")

    items = client.get("/api/build/work-items/history").json()["items"]
    row = next(it for it in items if it["title"] == "abandoned")
    assert row["outcome"] == "cancelled"


def test_history_clamps_limit(client: TestClient, db_session):
    for i in range(3):
        wid = make_test_work_item(title=f"shipped {i}", repo="ClawHaven/acme-app").id
        WorkItem.get(wid).update(pr_number=i + 1)
        _seed_op(db_session, wid, state="finished")
        _ship("ClawHaven/acme-app", i + 1)

    # limit > cap → clamps down, doesn't 400.
    response = client.get("/api/build/work-items/history?limit=10000")
    assert response.status_code == 200
    items = response.json()["items"]
    assert len(items) == 3  # all three shipped; cap doesn't truncate here

    # limit < 1 → clamps up to 1.
    response = client.get("/api/build/work-items/history?limit=0")
    assert response.status_code == 200
    items = response.json()["items"]
    assert len(items) == 1


def test_detail_timeline_shows_every_scope_run(db_session):
    # A detail page is history: re-scoping must surface all scope passes, not
    # just the latest (the bug — only _latest_scope_run was shown). Each pass is
    # its own run, distinct, not collapsed.
    from druks.durable.reads import list_subject_timeline

    item = make_test_work_item(
        title="rescoped", repo="ClawHaven/acme-app", source="linear", remote_key="ACME-777"
    )
    for _ in range(3):
        _seed_scope_run(db_session, item)

    entries = list_subject_timeline("work_item", str(item.id))
    assert [e.kind for e in entries] == ["build.scope"] * 3
    assert len({e.id for e in entries}) == 3  # distinct rows, not collapsed


def test_update_stamps_build_run_id(db_session):
    # build intake stamps the owning run via update(build_run_id=...); the kwarg
    # was missing, so every "Ready for Agent" transition threw a TypeError.
    from druks.build.models import WorkItem

    item = make_test_work_item(repo="ClawHaven/acme-app", title="x")
    run = seed_build_run(db_session, work_item_id=item.id, state="running")
    item.update(build_run_id=run.id)
    assert WorkItem.get(item.id).build_run_id == run.id


def test_timeline_shows_every_build_attempt(db_session):
    # Each build attempt is its own run; the timeline shows them all, with a
    # failed attempt's failure carried on its run.
    from druks.durable.reads import list_subject_timeline

    item = make_test_work_item(repo="ClawHaven/acme-app", title="x", remote_key="ACME-1")
    run1 = seed_build_run(db_session, work_item_id=item.id, state="failed", failure="boom")
    run2 = seed_build_run(db_session, work_item_id=item.id, state="failed")
    seed_call(db_session, run1, "generate_plan", status="failed", last_error="boom")
    seed_call(db_session, run2, "generate_plan", status="failed")

    entries = list_subject_timeline("work_item", str(item.id))
    assert len(entries) == 2
    assert all(e.agent_calls[0].agent == "generate_plan" for e in entries)
    assert any(e.failure == "boom" for e in entries)


async def test_subject_activity_surfaces_running_phase(db_session, monkeypatch):
    # A running build run pushes a transient phase; the detail view's live activity
    # surfaces it ("Building sandbox VM…") — finer than the lifecycle status.
    from druks.build import extension as build_extension

    item = make_test_work_item(repo="ClawHaven/acme-app", title="x")
    seed_build_run(db_session, work_item_id=item.id, state="running")

    async def phase(_run_id):
        return "provisioning_vm"

    monkeypatch.setattr(build_extension, "get_run_phase", phase)
    activity = await build_extension.Build.subject_activity(str(item.id))
    assert activity is not None
    assert activity.label == "Building sandbox VM…"
    assert activity.kind == "infra"


async def test_subject_activity_none_when_not_running(db_session, monkeypatch):
    # A run parked on a gate isn't working — no live sub-phase.
    from druks.build import extension as build_extension

    item = make_test_work_item(repo="ClawHaven/acme-app", title="x")
    seed_build_run(
        db_session, work_item_id=item.id, state="pending_input", input_gate="review_plan"
    )

    async def phase(_run_id):
        return "agent_running"

    monkeypatch.setattr(build_extension, "get_run_phase", phase)
    assert await build_extension.Build.subject_activity(str(item.id)) is None

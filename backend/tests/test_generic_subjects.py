from pathlib import Path

import pytest
from conftest import seed_dbos_status
from druks.durable import AgentCall, Run
from druks.durable.schemas import SubjectSummary
from druks.extensions.base import Extension
from fastapi import APIRouter
from fastapi.testclient import TestClient
from uuid_utils import uuid7


class _ThingSummary(SubjectSummary):
    title: str


class _ThingExtension(Extension):
    name = "faketest"
    subject_type = "thing"

    _THINGS = {"1": "First", "2": "Second"}

    @classmethod
    def subject_summary(cls, subject_id: str) -> _ThingSummary | None:
        title = cls._THINGS.get(subject_id)
        return _ThingSummary(id=subject_id, title=title) if title is not None else None

    @classmethod
    def list_subjects(cls) -> list[_ThingSummary]:
        return [_ThingSummary(id=sid, title=title) for sid, title in cls._THINGS.items()]


def _seed_run(
    session,
    *,
    subject_id,
    kind="faketest.flow",
    state="running",
    input_request=None,
    input_gate=None,
    failure=None,
):
    if state == "pending_input" and input_gate is None:
        input_gate = "review"
    run = Run(
        id=str(uuid7()),
        kind=kind,
        input_gate=input_gate,
        input_request=input_request,
        failure=failure,
    )
    session.add(run)
    session.flush()
    seed_dbos_status(session, run.id, state, subject={"type": "thing", "id": subject_id})
    return run


def _seed_call(session, run, *, agent, status="succeeded"):
    call = AgentCall(run_id=run.id, agent=agent, status=status, sandbox_host_id="h")
    session.add(call)
    session.flush()
    return call


@pytest.fixture
def client(tmp_path: Path, db_session, monkeypatch):
    # The real app mounts every extension's routers before its catch-all 404, so the
    # fake extension's router has to slot in there too — appending lands after the
    # catch-all and gets shadowed. Pulled back out on teardown; the app is a singleton.
    from conftest import configure_app_for_test, make_settings

    monkeypatch.setenv("DRUKS_DATA_DIR", str(tmp_path))
    app = configure_app_for_test(settings=make_settings(tmp_path))

    holder = APIRouter()
    holder.include_router(_ThingExtension._get_subject_routes(), prefix="/api/faketest")
    catchall = next(
        i for i, r in enumerate(app.routes) if getattr(r, "path", "") == "/api/{path:path}"
    )
    for route in reversed(holder.routes):
        app.router.routes.insert(catchall, route)
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        for route in holder.routes:
            app.router.routes.remove(route)


def test_status_aggregates_across_runs_and_timeline_spans_them(client: TestClient, db_session):
    # Subject "1" lived across two runs: an earlier finished one and a current
    # running one. Status is the active run's (running wins), and the timeline is
    # every run, oldest first, each carrying its own agent calls.
    done = _seed_run(db_session, subject_id="1", kind="faketest.scope", state="finished")
    _seed_call(db_session, done, agent="scope")
    live = _seed_run(db_session, subject_id="1", state="running")
    _seed_call(db_session, live, agent="implement", status="running")

    detail = client.get("/api/faketest/thing/1").json()
    assert detail["summary"] == {"id": "1", "title": "First"}
    assert detail["status"]["state"] == "running"
    assert [entry["kind"] for entry in detail["timeline"]] == ["faketest.scope", "faketest.flow"]
    # Calls group under their own run, not the subject at large.
    assert [c["agent"] for c in detail["timeline"][0]["agentCalls"]] == ["scope"]
    assert [c["agent"] for c in detail["timeline"][1]["agentCalls"]] == ["implement"]


def test_parked_run_surfaces_needs_you(client: TestClient, db_session):
    run = _seed_run(
        db_session,
        subject_id="1",
        state="pending_input",
        input_gate="approve_plan",
        input_request={"label": "Approve the plan"},
    )
    _seed_call(db_session, run, agent="generate_plan")

    detail = client.get("/api/faketest/thing/1").json()
    assert detail["status"]["state"] == "pending_input"
    assert detail["status"]["gate"] == "approve_plan"
    parked = detail["timeline"][-1]
    assert parked["inputRequest"] == {"label": "Approve the plan"}


def test_status_carries_the_latest_run_failure(client: TestClient, db_session):
    # A failed subject exposes its stop reason on the status, so a board can render
    # "why" without walking the timeline. An active or finished subject carries none.
    _seed_run(db_session, subject_id="1", state="failed", failure="profiler boom")

    status = client.get("/api/faketest/thing/1").json()["status"]
    assert status["state"] == "failed"
    assert status["failure"] == "profiler boom"

    _seed_run(db_session, subject_id="2", state="running")
    running = client.get("/api/faketest/thing/2").json()["status"]
    assert running["failure"] is None


def test_parked_board_row_skips_the_agent_call_query(client: TestClient, db_session, monkeypatch):
    # A parked row's status carries its gate ask, never its latest agent call, so
    # the per-subject status read must not query agent_calls — the board runs it
    # for every subject.
    run = _seed_run(
        db_session,
        subject_id="1",
        state="pending_input",
        input_gate="approve_plan",
        input_request={"label": "Approve the plan"},
    )
    _seed_call(db_session, run, agent="generate_plan")

    queried: list[str] = []
    monkeypatch.setattr(
        AgentCall,
        "list_for_run",
        classmethod(lambda cls, run_id: queried.append(run_id) or []),
    )

    rows = {row["summary"]["id"]: row for row in client.get("/api/faketest/thing").json()["rows"]}
    assert rows["1"]["status"]["gate"] == "approve_plan"
    assert queried == []


def test_list_returns_every_subject_with_status(client: TestClient, db_session):
    live = _seed_run(db_session, subject_id="1", state="running")
    _seed_call(db_session, live, agent="implement", status="running")

    body = client.get("/api/faketest/thing").json()
    rows = {row["summary"]["id"]: row for row in body["rows"]}
    assert rows["1"]["summary"]["title"] == "First"
    assert rows["1"]["status"]["state"] == "running"
    # "2" has no runs yet — it still lists, defaulting to scheduled.
    assert rows["2"]["status"]["state"] == "scheduled"


def test_unknown_subject_is_404(client: TestClient, db_session):
    assert client.get("/api/faketest/thing/nope").status_code == 404

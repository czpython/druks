from datetime import UTC, datetime
from pathlib import Path

import pytest
from conftest import (
    configure_app_for_test,
    make_settings,
    make_test_work_item,
    seed_build_run,
    seed_call,
    seed_run,
)
from druks.accounts.models import Account
from druks.build import agent as build_agent
from druks.build.workflows import BuildWorkflow
from druks.durable import agent as durable_agent
from druks.durable.models import Run
from druks.durable.reads import read_transcript_chunk
from druks.usage import agent as usage_agent
from fastapi.testclient import TestClient

_IN_APP_ASK = {
    "presentation": "in_app",
    "controls": ["approve", "request_changes", "cancel"],
    "questions": [],
}

_AGENT_ROUTES = {
    ("get", "/api/build/agent/work"): "list_work",
    ("get", "/api/build/agent/work-items/{work_item_id}"): "get_work_item",
    ("post", "/api/build/agent/dispatch"): "dispatch",
    ("get", "/api/agent/gates/{run_id}"): "get_gate",
    ("post", "/api/agent/gates/{run_id}/answer"): "answer_gate",
    ("get", "/api/agent/agent-calls/{call_id}"): "get_agent_call",
    ("post", "/api/agent/runs/{run_id}/cancel"): "cancel_run",
    ("get", "/api/usage/agent"): "get_usage",
}


@pytest.fixture
def client(tmp_path: Path, db_session, monkeypatch):
    monkeypatch.setenv("DRUKS_DATA_DIR", str(tmp_path))
    app = configure_app_for_test(settings=make_settings(tmp_path))
    with TestClient(app) as client:
        yield client


@pytest.fixture
def account(db_session):
    # The account configure_app_for_test signs requests in as.
    return Account.get_or_create("op@example.com")


@pytest.fixture
def resume_spy(monkeypatch):
    calls = []

    async def _spy(self, **fields):
        calls.append({"id": self.id, **fields})

    monkeypatch.setattr(Run, "resume", _spy)
    return calls


def _park(db_session, item_id):
    run = seed_build_run(
        db_session,
        work_item_id=item_id,
        state="pending_input",
        input_gate="review",
        input_request=dict(_IN_APP_ASK),
    )
    run.input_requested_at = datetime.now(UTC)
    db_session.flush()
    return run


def test_openapi_pins_the_eight_agent_routes(client: TestClient):
    from druks.api.app import app

    schema = app.openapi()
    found = {
        (method, path): operation
        for path, operations in schema["paths"].items()
        for method, operation in operations.items()
        if operation.get("tags") == ["agent"]
    }
    assert {key: op["operationId"] for key, op in found.items()} == _AGENT_ROUTES


def test_agent_routes_sit_behind_the_gate(tmp_path, db_session):
    app = configure_app_for_test(settings=make_settings(tmp_path), authenticated=False)
    with TestClient(app) as anonymous:
        assert anonymous.get("/api/build/agent/work").status_code == 401
        assert anonymous.get("/api/agent/gates/x").status_code == 401
        assert anonymous.get("/api/usage/agent").status_code == 401


def test_agent_errors_share_one_shape(client: TestClient, db_session):
    missing = client.get("/api/agent/gates/no-such-run")
    assert missing.status_code == 404
    assert missing.json() == {
        "code": "RUN_NOT_FOUND",
        "message": "No run no-such-run.",
        "retryable": False,
    }

    bad_cursor = client.get("/api/build/agent/work", params={"cursor": "!!junk!!"})
    assert bad_cursor.status_code == 400
    assert bad_cursor.json()["code"] == "INVALID_CURSOR"

    item = make_test_work_item(repo="o/r", title="t")
    run = _park(db_session, item.id)
    stale = client.post(
        f"/api/agent/gates/{run.id}/answer",
        json={"parkedAt": "2020-01-01T00:00:00+00:00", "control": "approve"},
    )
    assert stale.status_code == 409
    body = stale.json()
    assert body["code"] == "GATE_ROUND_STALE"
    assert body["retryable"] is True

    missing_item = client.get("/api/build/agent/work-items/999999")
    assert missing_item.status_code == 404
    assert missing_item.json()["code"] == "WORK_ITEM_NOT_FOUND"


def test_get_gate_then_answer_roundtrip(client: TestClient, db_session, resume_spy):
    item = make_test_work_item(repo="o/r", title="t")
    run = _park(db_session, item.id)

    view = client.get(f"/api/agent/gates/{run.id}")
    assert view.status_code == 200
    data = view.json()
    assert data == durable_agent.get_gate(run.id).model_dump(mode="json", by_alias=True)

    answered = client.post(
        f"/api/agent/gates/{run.id}/answer",
        json={"parkedAt": data["parkedAt"], "control": "approve", "note": "ship it"},
    )
    assert answered.status_code == 200
    assert answered.json()["result"] == "answered"
    assert resume_spy == [{"id": run.id, "action": "approve", "answers": {}, "note": "ship it"}]


def test_answer_gate_reads_already_answered_off_the_receipt(
    client: TestClient, db_session, resume_spy
):
    item = make_test_work_item(repo="o/r", title="t")
    parked_at = datetime.now(UTC)
    run = seed_build_run(db_session, work_item_id=item.id, state="running")
    run.input_requested_at = parked_at
    run.answered_parked_at = parked_at
    db_session.flush()

    response = client.post(
        f"/api/agent/gates/{run.id}/answer",
        json={"parkedAt": parked_at.isoformat(), "control": "approve"},
    )

    assert response.status_code == 200
    assert response.json()["result"] == "already_answered"
    assert resume_spy == []


def test_answer_gate_requires_an_aware_parked_at(client: TestClient, db_session):
    item = make_test_work_item(repo="o/r", title="t")
    run = _park(db_session, item.id)

    naive = client.post(
        f"/api/agent/gates/{run.id}/answer",
        json={"parkedAt": "2026-07-19T10:00:00", "control": "approve"},
    )

    assert naive.status_code == 422  # Pydantic's, not the agent taxonomy


def test_cancel_run_route(client: TestClient, db_session):
    item = make_test_work_item(repo="o/r", title="t")
    run = seed_build_run(db_session, work_item_id=item.id, state="running")
    # Parked, so the cancel must clear the gate — and never write the receipt.
    run.input_gate = "review"
    run.input_request = {"presentation": "in_app", "questions": []}
    run.input_requested_at = run.utc_now()
    db_session.flush()

    unbounded = client.post(f"/api/agent/runs/{run.id}/cancel", json={"reason": "r" * 501})
    assert unbounded.status_code == 422
    blank = client.post(f"/api/agent/runs/{run.id}/cancel", json={"reason": ""})
    assert blank.status_code == 422

    cancelled = client.post(f"/api/agent/runs/{run.id}/cancel", json={"reason": "wrong branch"})
    assert cancelled.status_code == 200
    assert cancelled.json() == {"runId": run.id, "result": "cancelled"}

    db_session.expire_all()
    run = db_session.get(type(run), run.id)
    assert not run.answered_parked_at
    assert not run.input_gate
    assert run.failure == "wrong branch"

    again = client.post(f"/api/agent/runs/{run.id}/cancel", json={"reason": "wrong branch"})
    assert again.status_code == 200
    assert again.json()["result"] == "already_cancelled"


def test_dispatch_route(client: TestClient, db_session, account, monkeypatch):
    item = make_test_work_item(repo="o/r", title="t")
    seed_run(db_session, "run-dispatch", account_id=account.id)

    async def _start(cls, **kwargs):
        return "run-dispatch"

    monkeypatch.setattr(BuildWorkflow, "start", classmethod(_start))

    both = client.post(
        "/api/build/agent/dispatch",
        json={"work_item_id": item.id, "source": "linear", "ticket_ref": "ACME-1"},
    )
    assert both.status_code == 422
    neither = client.post("/api/build/agent/dispatch", json={})
    assert neither.status_code == 422
    half = client.post("/api/build/agent/dispatch", json={"source": "linear"})
    assert half.status_code == 422

    ok = client.post("/api/build/agent/dispatch", json={"work_item_id": item.id})
    assert ok.status_code == 200
    body = ok.json()
    assert body["workItemId"] == item.id
    assert body["runId"] == "run-dispatch"
    assert body["isOwnedByCaller"] is True


def test_work_routes_match_the_services(client: TestClient, db_session, account):
    item = make_test_work_item(repo="o/r", title="parity", remote_key="ACME-9")
    run = seed_build_run(db_session, work_item_id=item.id, state="running")
    seed_call(db_session, run, "implement")

    page = client.get("/api/build/agent/work")
    assert page.status_code == 200
    assert page.json() == build_agent.list_work(account).model_dump(mode="json", by_alias=True)

    detail = client.get(f"/api/build/agent/work-items/{item.id}")
    assert detail.status_code == 200
    assert detail.json() == build_agent.get_work_item(item.id).model_dump(
        mode="json", by_alias=True
    )

    call_id = detail.json()["runs"][0]["agentCalls"][0]["id"]
    call = client.get(f"/api/agent/agent-calls/{call_id}")
    assert call.status_code == 200
    assert call.json() == durable_agent.get_agent_call(call_id).model_dump(
        mode="json", by_alias=True
    )


def test_board_status_matches_list_work(client: TestClient, db_session, account):
    item = make_test_work_item(repo="o/r", title="board parity")
    _park(db_session, item.id)

    board = client.get("/api/build/work_item")
    assert board.status_code == 200
    row = next(r for r in board.json()["rows"] if r["summary"]["id"] == str(item.id))
    listed = next(
        i
        for i in build_agent.list_work(account).model_dump(mode="json", by_alias=True)["items"]
        if i["workItemId"] == item.id
    )
    assert listed["status"] == row["status"]


def test_transcript_route_matches_the_read_machinery(client: TestClient, db_session, db_engine):
    from conftest import seed_agent_run

    call = seed_agent_run()
    call_dir = call.call_dir
    call_dir.mkdir(parents=True, exist_ok=True)
    (call_dir / "stdout.jsonl").write_bytes(b"hello " + "é".encode() + b" transcript")

    response = client.get(
        f"/api/build/transcripts/{call.id}", params={"stream": "stdout", "limit": 7}
    )
    assert response.status_code == 200
    chunk = read_transcript_chunk(db_engine, call.id, "stdout", offset=0, limit=7)
    assert response.json() == chunk.model_dump(mode="json", by_alias=True)
    # The 7-byte cut lands mid-é; the served chunk ends on a character boundary.
    assert response.json()["text"] == "hello "


def test_resume_route_contract_is_preserved(client: TestClient, db_session, resume_spy):
    unknown = client.post("/api/runs/no-such-run/resume", json={"control": "approve"})
    assert unknown.status_code == 404

    item = make_test_work_item(repo="o/r", title="t")
    idle = seed_build_run(db_session, work_item_id=item.id, state="running")
    not_waiting = client.post(f"/api/runs/{idle.id}/resume", json={"control": "approve"})
    assert not_waiting.status_code == 409

    parked_item = make_test_work_item(repo="o/r2", title="t")
    run = _park(db_session, parked_item.id)
    bad_control = client.post(f"/api/runs/{run.id}/resume", json={"control": "merge"})
    assert bad_control.status_code == 422
    assert resume_spy == []

    ok = client.post(
        f"/api/runs/{run.id}/resume",
        json={"control": "approve", "answers": {}, "note": "go"},
    )
    assert ok.status_code == 204
    assert resume_spy == [{"id": run.id, "action": "approve", "answers": {}, "note": "go"}]

    # Once the answer has landed (receipt written, gate cleared), the
    # dashboard's double-submit stays the conflict it has always been.
    run.answered_parked_at = run.input_requested_at
    run.input_gate = None
    run.input_request = None
    db_session.flush()
    late = client.post(f"/api/runs/{run.id}/resume", json={"control": "approve"})
    assert late.status_code == 409
    assert len(resume_spy) == 1


def test_usage_agent_route_matches_the_service(client: TestClient, db_session, account):
    from druks.durable.models import AgentCall

    run = seed_run(db_session, "run-usage-route")
    db_session.add(
        AgentCall(
            run_id=run.id,
            account_id=account.id,
            sandbox_host_id="host",
            model="gpt-5.5",
            status="succeeded",
            finished_at=datetime.now(UTC),
            cost_usd=1.25,
            cost_metadata={"total_tokens": 500},
        )
    )
    db_session.flush()

    response = client.get("/api/usage/agent")
    assert response.status_code == 200
    body = response.json()
    assert body == usage_agent.get_usage(account).model_dump(mode="json", by_alias=True)
    assert len(response.content) <= 4 * 1024

    today = client.get("/api/usage/today").json()
    assert sum(h["spendUsd"] for h in today["harnesses"]) == pytest.approx(body["spendTodayUsd"])
    assert sum(h["runs"] for h in today["harnesses"]) == body["runsToday"]
    assert sum(h["tokens"] for h in today["harnesses"]) == body["tokensToday"]
    assert today["day"] == body["day"]

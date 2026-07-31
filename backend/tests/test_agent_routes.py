from datetime import UTC, datetime
from pathlib import Path

import pytest
from druks.accounts.models import Account
from druks.api.app import app
from druks.durable.models import Run
from druks.durable.reads import read_transcript_chunk
from druks.mcp.gateway import services
from druks.testing import configure_app_for_test, make_settings, seed_call, seed_run
from druks_field_notes.models import Note
from druks_field_notes.workflows import Summarize
from fastapi.testclient import TestClient

_IN_APP_ASK = {
    "presentation": "in_app",
    "controls": ["approve", "request_changes"],
    "questions": [],
}

_MCP_ROUTES = {
    ("get", "/api/gates/{run_id}"): "get_gate",
    ("post", "/api/gates/{run_id}/answer"): "answer_gate",
    ("get", "/api/agent-calls/{call_id}"): "get_agent_call",
    ("post", "/api/runs/{run_id}/cancel"): "cancel_run",
    ("post", "/api/runs/{run_id}/retry"): "retry_run",
    ("get", "/api/usage/summary"): "get_usage",
}


@pytest.fixture
def client(tmp_path: Path, druks_db, monkeypatch):
    monkeypatch.setenv("DRUKS_DATA_DIR", str(tmp_path))
    app = configure_app_for_test(settings=make_settings(tmp_path))
    with TestClient(app) as client:
        yield client


@pytest.fixture
def account(druks_db):
    # The account configure_app_for_test signs requests in as.
    return Account.get_or_create("op@example.com")


@pytest.fixture
def resume_spy(monkeypatch):
    calls = []

    async def _spy(self, **fields):
        calls.append({"id": self.id, **fields})

    monkeypatch.setattr(Run, "resume", _spy)
    return calls


def _park(druks_db, note):
    run = seed_run(
        druks_db,
        kind=Summarize.kind,
        subject=note,
        state="parked",
        input_gate="review",
        input_request=dict(_IN_APP_ASK),
    )
    run.input_requested_at = datetime.now(UTC)
    druks_db.flush()
    return run


def test_openapi_pins_the_six_agent_routes(client: TestClient):
    schema = app.openapi()
    found = {
        (method, path): operation
        for path, operations in schema["paths"].items()
        for method, operation in operations.items()
        if operation.get("tags") == ["agent"]
    }
    assert {key: op["operationId"] for key, op in found.items()} == _MCP_ROUTES


def test_agent_routes_sit_behind_the_gate(tmp_path, druks_db):
    # Header mode: an unasserted request is a 401, not none-mode's setup 409.
    app = configure_app_for_test(
        settings=make_settings(tmp_path, auth_mode="header", auth_header="X-Edge-Email"),
        authenticated=False,
    )
    with TestClient(app) as anonymous:
        assert anonymous.get("/api/gates/x").status_code == 401
        assert anonymous.get("/api/usage/summary").status_code == 401


def test_agent_errors_share_one_shape(client: TestClient, druks_db):
    missing = client.get("/api/gates/no-such-run")
    assert missing.status_code == 404
    assert missing.json() == {
        "code": "RUN_NOT_FOUND",
        "message": "No run no-such-run.",
        "retryable": False,
    }

    note = Note.create(body="stale gate")
    run = _park(druks_db, note)
    stale = client.post(
        f"/api/gates/{run.id}/answer",
        json={"parkedAt": "2020-01-01T00:00:00+00:00", "control": "approve"},
    )
    assert stale.status_code == 409
    body = stale.json()
    assert body["code"] == "GATE_ROUND_STALE"
    assert body["retryable"] is True


def test_get_gate_then_answer_roundtrip(client: TestClient, druks_db, resume_spy):
    note = Note.create(body="answer gate")
    run = _park(druks_db, note)

    view = client.get(f"/api/gates/{run.id}")
    assert view.status_code == 200
    data = view.json()
    assert data == services.get_gate(run.id).model_dump(mode="json", by_alias=True)

    answered = client.post(
        f"/api/gates/{run.id}/answer",
        json={"parkedAt": data["parkedAt"], "control": "approve", "note": "ship it"},
    )
    assert answered.status_code == 200
    assert answered.json()["result"] == "answered"
    assert resume_spy == [{"id": run.id, "action": "approve", "answers": {}, "note": "ship it"}]


def test_answer_gate_reads_already_answered_off_the_receipt(
    client: TestClient, druks_db, resume_spy
):
    note = Note.create(body="answered gate")
    parked_at = datetime.now(UTC)
    run = seed_run(druks_db, kind=Summarize.kind, subject=note)
    run.input_requested_at = parked_at
    run.answer_parked_at = parked_at
    druks_db.flush()

    response = client.post(
        f"/api/gates/{run.id}/answer",
        json={"parkedAt": parked_at.isoformat(), "control": "approve"},
    )

    assert response.status_code == 200
    assert response.json()["result"] == "already_answered"
    assert resume_spy == []


def test_answer_gate_requires_an_aware_parked_at(client: TestClient, druks_db):
    note = Note.create(body="naive parked timestamp")
    run = _park(druks_db, note)

    naive = client.post(
        f"/api/gates/{run.id}/answer",
        json={"parkedAt": "2026-07-19T10:00:00", "control": "approve"},
    )

    assert naive.status_code == 422  # Pydantic's, not the agent taxonomy


def test_cancel_run_route(client: TestClient, druks_db):
    note = Note.create(body="cancelled note")
    run = seed_run(druks_db, kind=Summarize.kind, subject=note)
    # Parked, so the cancel must clear the gate — and never write the receipt.
    run.input_gate = "review"
    run.input_request = {"presentation": "in_app", "questions": []}
    run.input_requested_at = run.utc_now()
    druks_db.flush()

    unbounded = client.post(f"/api/runs/{run.id}/cancel", json={"reason": "r" * 501})
    assert unbounded.status_code == 422
    blank = client.post(f"/api/runs/{run.id}/cancel", json={"reason": ""})
    assert blank.status_code == 422

    cancelled = client.post(f"/api/runs/{run.id}/cancel", json={"reason": "wrong branch"})
    assert cancelled.status_code == 200
    assert cancelled.json() == {"runId": run.id, "result": "cancelled"}

    druks_db.expire_all()
    run = druks_db.get(type(run), run.id)
    assert not run.answer_parked_at
    assert not run.input_gate
    assert run.failure == "wrong branch"

    again = client.post(f"/api/runs/{run.id}/cancel", json={"reason": "wrong branch"})
    assert again.status_code == 200
    assert again.json()["result"] == "already_cancelled"


def test_transcript_route_matches_the_read_machinery(client: TestClient, druks_db):
    note = Note.create(body="transcript route")
    run = seed_run(druks_db, kind=Summarize.kind, subject=note)
    call = seed_call(druks_db, run, "summarize", status="running")
    call_dir = call.call_dir
    call_dir.mkdir(parents=True, exist_ok=True)
    (call_dir / "stdout.jsonl").write_bytes(b"hello " + "é".encode() + b" transcript")

    response = client.get(
        f"/api/field_notes/transcripts/{call.id}",
        params={"stream": "stdout", "limit": 7},
    )
    assert response.status_code == 200
    chunk = read_transcript_chunk(call, "stdout", offset=0, limit=7)
    assert response.json() == chunk.model_dump(mode="json", by_alias=True)
    # The 7-byte cut lands mid-é; the window serves the seam's one �.
    assert response.json()["text"] == "hello �"


def test_resume_route_contract_is_preserved(client: TestClient, druks_db, resume_spy):
    unknown = client.post("/api/runs/no-such-run/resume", json={"control": "approve"})
    assert unknown.status_code == 404

    idle_note = Note.create(body="idle run")
    idle = seed_run(druks_db, kind=Summarize.kind, subject=idle_note)
    not_waiting = client.post(f"/api/runs/{idle.id}/resume", json={"control": "approve"})
    assert not_waiting.status_code == 409

    parked_note = Note.create(body="parked run")
    run = _park(druks_db, parked_note)
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
    run.answer_parked_at = run.input_requested_at
    run.input_gate = None
    run.input_request = None
    druks_db.flush()
    late = client.post(f"/api/runs/{run.id}/resume", json={"control": "approve"})
    assert late.status_code == 409
    assert len(resume_spy) == 1


def test_usage_agent_route_matches_the_service(client: TestClient, druks_db, account):
    from druks.durable.models import AgentCall

    note = Note.create(body="usage route")
    run = seed_run(
        druks_db,
        kind=Summarize.kind,
        subject=note,
        run_id="run-usage-route",
    )
    druks_db.add(
        AgentCall(
            run_id=run.id,
            agent="summarize",
            account_id=account.id,
            sandbox_host_id="host",
            model="gpt-5.5",
            status="succeeded",
            finished_at=datetime.now(UTC),
            cost_usd=1.25,
            cost_metadata={"total_tokens": 500},
        )
    )
    druks_db.flush()

    response = client.get("/api/usage/summary")
    assert response.status_code == 200
    body = response.json()
    assert body == services.get_usage(account).model_dump(mode="json", by_alias=True)
    assert len(response.content) <= 4 * 1024

    today = client.get("/api/usage/today").json()
    assert sum(h["spendUsd"] for h in today["harnesses"]) == pytest.approx(body["spendTodayUsd"])
    assert sum(h["runs"] for h in today["harnesses"]) == body["runsToday"]
    assert sum(h["tokens"] for h in today["harnesses"]) == body["tokensToday"]
    assert today["day"] == body["day"]

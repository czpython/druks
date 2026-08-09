from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from druks.accounts.models import Account, PersonalAccessToken
from druks.api.app import app
from druks.contrib.review.workflows import PullRequestReview
from druks.contrib.ship.extension import Ship
from druks.contrib.ship.models import Project, ProjectRepo, WorkItem
from druks.contrib.ship.ticketing.enums import TicketStatus
from druks.contrib.ship.ticketing.exceptions import LinearAPIError, UnknownTicketError
from druks.contrib.ship.workflows import Build
from druks.durable.dbos_state import workflow_status
from druks.durable.models import AgentCall, Run
from druks.durable.reads import read_transcript_chunk
from druks.mcp.gateway import services
from druks.service_identities.models import ServiceIdentity
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
    ("get", "/api/gates/{run}"): "get_gate",
    ("post", "/api/gates/{run}/answer"): "answer_gate",
    ("get", "/api/agent-calls/{call}"): "get_agent_call",
    ("post", "/api/runs/{run}/cancel"): "cancel_run",
    ("post", "/api/runs/{run}/retry"): "retry_run",
    ("get", "/api/open-subjects"): "list_open_subjects",
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


def _connect_github() -> None:
    # Start routes guard on the GitHub service identity before spending a run.
    ServiceIdentity.connect(
        "github",
        identity={"app_id": "1", "slug": "druks-operator"},
        secrets={"private_key": "operator-pem", "webhook_secret": "hook-secret"},
    )


@pytest.fixture
def resume_spy(monkeypatch):
    calls = []

    async def _spy(self, **fields):
        calls.append({"id": self.id, **fields})

    monkeypatch.setattr(Run, "resume", _spy)
    return calls


def _park(druks_db, note, *, context: str = ""):
    ask = dict(_IN_APP_ASK)
    if context:
        ask["context"] = context
    run = seed_run(
        druks_db,
        kind=Summarize.kind,
        subject=note,
        state="parked",
        input_gate="review",
        input_request=ask,
    )
    run.input_requested_at = datetime.now(UTC)
    druks_db.flush()
    return run


def test_openapi_pins_platform_and_extension_agent_routes(client: TestClient):
    schema = app.openapi()
    found = {
        (method, path): operation
        for path, operations in schema["paths"].items()
        for method, operation in operations.items()
        if "agent" in operation.get("tags", [])
    }
    assert {key: found[key]["operationId"] for key in _MCP_ROUTES} == _MCP_ROUTES
    assert found[("post", "/api/review/reviews")]["operationId"] == "review_request"
    assert found[("post", "/api/ship/work-items/{ticket}/start")]["operationId"] == "ship_start"

    extensions = {
        key: {name: value for name, value in found[key].items() if name.startswith("x-")}
        for key in _MCP_ROUTES
    }
    assert extensions == {
        ("get", "/api/gates/{run}"): {},
        ("post", "/api/gates/{run}/answer"): {
            "x-destructive": False,
            "x-idempotent": True,
        },
        ("get", "/api/agent-calls/{call}"): {},
        ("post", "/api/runs/{run}/cancel"): {"x-idempotent": True},
        ("post", "/api/runs/{run}/retry"): {"x-destructive": False},
        ("get", "/api/open-subjects"): {},
        ("get", "/api/usage/summary"): {},
    }
    assert not {name for name in found[("post", "/api/review/reviews")] if name.startswith("x-")}
    assert not {
        name
        for name in found[("post", "/api/ship/work-items/{ticket}/start")]
        if name.startswith("x-")
    }


def test_review_request_returns_the_run_id_start_hands_back(
    client: TestClient, account: Account, monkeypatch
):
    _connect_github()
    project = Project.create(name="Acme")
    ProjectRepo.create(project_id=project.id, full_name="acme/app")
    live_run_id = "review-run-id"
    starts = []

    async def start(cls, **kwargs):
        starts.append(kwargs)
        return live_run_id

    monkeypatch.setattr(PullRequestReview, "start", classmethod(start))

    responses = [
        client.post(
            "/api/review/reviews",
            json={"repo": "acme/app", "prNumber": 7},
        )
        for _ in range(2)
    ]

    assert [response.status_code for response in responses] == [202, 202]
    assert [response.json() for response in responses] == [live_run_id, live_run_id]
    assert [call["subject"].identity for call in starts] == [
        {"type": "pull_request", "id": "acme/app#7"},
        {"type": "pull_request", "id": "acme/app#7"},
    ]
    assert {call["account_id"] for call in starts} == {account.id}


class _FakeTracker:
    """Records the status moves the route delegates to — no HTTP."""

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.aclose()

    async def set_status(self, key, status):
        self.calls.append((key, status))
        if self.error:
            raise self.error

    async def aclose(self):
        self.calls.append("aclose")


def test_ship_start_stamps_the_trigger_status_for_known_and_unknown_tickets(
    client: TestClient, account: Account, monkeypatch
):
    # ENG-831 has a local work item, ENG-777 has never been seen — both take
    # the same tracker path; webhook intake, not the route, opens builds.
    project = Project.create(name="Acme")
    ProjectRepo.create(project_id=project.id, full_name="acme/app")
    WorkItem.create(
        project_id=project.id,
        source="linear",
        title="Build the agent route",
        ticket_key="ENG-831",
        repo="acme/app",
    )
    _, pat_token = PersonalAccessToken.create(account_id=account.id, name="agent")
    fake = _FakeTracker()
    monkeypatch.setattr(Ship, "get_tracker", classmethod(lambda cls, source=None: fake))
    starts = []

    async def start(cls, **kwargs):
        starts.append(kwargs)

    monkeypatch.setattr(Build, "start", classmethod(start))

    responses = [
        client.post(
            f"/api/ship/work-items/{ticket}/start",
            headers={"Authorization": f"Bearer {pat_token}"},
        )
        for ticket in ("ENG-831", "ENG-777", "ENG-831")
    ]

    assert [response.status_code for response in responses] == [202, 202, 202]
    # Accepted-and-pending: no run id, no invented acknowledgement fields.
    assert [response.json() for response in responses] == [None, None, None]
    assert fake.calls == [
        ("ENG-831", TicketStatus.TRIGGER),
        "aclose",
        ("ENG-777", TicketStatus.TRIGGER),
        "aclose",
        ("ENG-831", TicketStatus.TRIGGER),
        "aclose",
    ]
    assert starts == []  # a repeat call re-stamps; it never dispatches locally


def test_ship_start_translates_an_unknown_tracker_ticket(client: TestClient, monkeypatch):
    fake = _FakeTracker(error=UnknownTicketError("ENG-9999", "Linear"))
    monkeypatch.setattr(Ship, "get_tracker", classmethod(lambda cls, source=None: fake))

    response = client.post("/api/ship/work-items/ENG-9999/start")

    assert response.status_code == 404
    assert response.json() == {
        "code": "TICKET_NOT_FOUND",
        "message": "ENG-9999 doesn't exist in Linear",
        "retryable": False,
    }
    assert "aclose" in fake.calls  # the tracker closes even on failure


def test_ship_start_reports_a_missing_tracker_configuration(client: TestClient):
    # Default settings select Linear but carry no API key, so no tracker
    # resolves — the operator-configuration error, not a 404 or a 500.
    response = client.post("/api/ship/work-items/ENG-1/start")

    assert response.status_code == 503
    body = response.json()
    assert body["code"] == "TRACKER_NOT_CONFIGURED"
    assert body["retryable"] is False


def test_ship_start_does_not_acknowledge_a_tracker_failure(tmp_path, druks_db, monkeypatch):
    monkeypatch.setenv("DRUKS_DATA_DIR", str(tmp_path))
    app = configure_app_for_test(settings=make_settings(tmp_path))
    fake = _FakeTracker(error=LinearAPIError("linear fell over"))
    monkeypatch.setattr(Ship, "get_tracker", classmethod(lambda cls, source=None: fake))

    with TestClient(app, raise_server_exceptions=False) as failing:
        response = failing.post("/api/ship/work-items/ENG-831/start")

    assert response.status_code == 500
    assert response.json() == {"error": "INTERNAL_ERROR", "detail": "Internal server error"}


def test_review_request_refuses_when_github_is_not_connected(client: TestClient, monkeypatch):
    project = Project.create(name="Acme")
    ProjectRepo.create(project_id=project.id, full_name="acme/app")

    async def start(cls, **kwargs):
        raise AssertionError("start must not be reached without a GitHub identity")

    monkeypatch.setattr(PullRequestReview, "start", classmethod(start))

    response = client.post("/api/review/reviews", json={"repo": "acme/app", "prNumber": 7})

    assert response.status_code == 409
    assert "not connected" in response.json()["detail"]


def test_agent_routes_sit_behind_the_gate(tmp_path, druks_db):
    # Header mode: an unasserted request is a 401, not none-mode's setup 409.
    app = configure_app_for_test(
        settings=make_settings(
            tmp_path,
            identity={"mode": "header", "header": "X-Edge-Email"},
        ),
        authenticated=False,
    )
    with TestClient(app) as anonymous:
        assert anonymous.get("/api/gates/x").status_code == 401
        assert anonymous.get("/api/open-subjects").status_code == 401
        assert anonymous.get("/api/usage/summary").status_code == 401
        assert anonymous.post("/api/ship/work-items/ENG-831/start").status_code == 401


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


def test_missing_agent_call_uses_the_unified_shape(client: TestClient, druks_db):
    response = client.get("/api/agent-calls/missing")

    assert response.status_code == 404
    assert response.json() == {
        "code": "AGENT_CALL_NOT_FOUND",
        "message": "No agent call missing.",
        "retryable": False,
    }


def test_list_open_subjects_returns_newest_open_work_and_latest_calls(client: TestClient, druks_db):
    finished_note = Note.create(body="finished")
    seed_run(druks_db, kind=Summarize.kind, subject=finished_note, state="finished")

    failed_note = Note.create(body="failed")
    older = seed_run(druks_db, kind=Summarize.kind, subject=failed_note)
    older.created_at = datetime(2026, 1, 1, tzinfo=UTC)
    seed_call(druks_db, older, "older")
    failure = "discarded failure prefix " + "f" * 512
    newest = seed_run(
        druks_db,
        kind=Summarize.kind,
        subject=failed_note,
        state="failed",
        failure=failure,
    )
    newest.created_at = older.created_at + timedelta(days=1)
    seed_call(druks_db, newest, "first")
    latest_call = seed_call(druks_db, newest, "latest")
    subject_label = "long label kept whole " + "l" * 512
    druks_db.execute(
        workflow_status.update()
        .where(workflow_status.c.workflow_uuid == newest.id)
        .values(
            attributes={
                "subject_type": failed_note.subject_type,
                "subject_id": str(failed_note.id),
                "subject_label": subject_label,
            }
        )
    )

    callless_note = Note.create(body="callless")
    seed_run(druks_db, kind="field_notes.audit", subject=callless_note)
    seed_run(druks_db, kind="usage.scrape")

    response = client.get("/api/open-subjects")

    assert response.status_code == 200
    body = response.json()
    subjects = {subject["subjectId"]: subject for subject in body["subjects"]}
    assert set(subjects) == {str(failed_note.id), str(callless_note.id)}
    assert subjects[str(failed_note.id)] == {
        "subjectType": failed_note.subject_type,
        "subjectId": str(failed_note.id),
        "subjectLabel": subject_label,
        "workflows": [
            {
                "extension": "field_notes",
                "state": "failed",
                "run": newest.id,
                "latestAgentCall": latest_call.id,
                "failure": "f" * 512,
                "createdAt": newest.created_at.isoformat().replace("+00:00", "Z"),
            }
        ],
    }
    assert subjects[str(callless_note.id)]["workflows"][0]["latestAgentCall"] is None


def test_list_open_subjects_keeps_type_and_kind_partitions(client: TestClient, druks_db):
    typed_note = Note.create(body="two types")
    note_run = seed_run(druks_db, kind=Summarize.kind, subject=typed_note)
    ticket_run = seed_run(druks_db, kind=Summarize.kind, subject=typed_note)
    druks_db.execute(
        workflow_status.update()
        .where(workflow_status.c.workflow_uuid == ticket_run.id)
        .values(
            attributes={
                "subject_type": "ticket",
                "subject_id": str(typed_note.id),
                "subject_label": "T-1",
            }
        )
    )

    multi_kind_note = Note.create(body="two kinds")
    scan = seed_run(druks_db, kind="field_notes.scan", subject=multi_kind_note)
    audit = seed_run(druks_db, kind="field_notes.audit", subject=multi_kind_note)

    terminal_sibling_note = Note.create(body="terminal sibling")
    failed_scan = seed_run(
        druks_db,
        kind="field_notes.scan",
        subject=terminal_sibling_note,
        state="failed",
    )
    failed_scan.created_at = datetime(2026, 1, 1, tzinfo=UTC)
    finished_audit = seed_run(
        druks_db,
        kind="field_notes.audit",
        subject=terminal_sibling_note,
        state="finished",
    )
    finished_audit.created_at = failed_scan.created_at + timedelta(days=1)
    druks_db.flush()

    body = client.get("/api/open-subjects").json()

    assert len(body["subjects"]) == 4
    run_ids = {workflow["run"] for subject in body["subjects"] for workflow in subject["workflows"]}
    assert run_ids == {note_run.id, ticket_run.id, scan.id, audit.id, failed_scan.id}
    assert finished_audit.id not in run_ids
    multi_kind = next(
        subject
        for subject in body["subjects"]
        if subject["subjectId"] == str(multi_kind_note.id)
        and subject["subjectType"] == multi_kind_note.subject_type
    )
    assert {workflow["run"] for workflow in multi_kind["workflows"]} == {scan.id, audit.id}
    shared_id_types = {
        subject["subjectType"]
        for subject in body["subjects"]
        if subject["subjectId"] == str(typed_note.id)
    }
    assert shared_id_types == {typed_note.subject_type, "ticket"}


def test_list_open_subjects_excludes_historical_runs(client: TestClient, druks_db):
    note = Note.create(body="one open subject")
    start = datetime(2026, 1, 1, tzinfo=UTC)
    for number in range(10):
        historical = seed_run(druks_db, kind=Summarize.kind, subject=note, state="finished")
        historical.created_at = start + timedelta(seconds=number)
    current = seed_run(druks_db, kind=Summarize.kind, subject=note)
    current.created_at = start + timedelta(seconds=10)
    druks_db.flush()

    body = client.get("/api/open-subjects").json()

    assert [
        workflow["run"] for subject in body["subjects"] for workflow in subject["workflows"]
    ] == [current.id]


def test_list_open_subjects_caps_the_workflows(client: TestClient, druks_db):
    for number in range(51):
        note = Note.create(body=f"open {number}")
        seed_run(druks_db, kind=Summarize.kind, subject=note)

    body = client.get("/api/open-subjects").json()

    assert len(body["subjects"]) == 50


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


def test_answer_gate_keys_empty_request_changes_on_ask_context(
    client: TestClient, druks_db, resume_spy
):
    critique_note = Note.create(body="critique-backed gate")
    critique_run = _park(druks_db, critique_note, context="name the rollback boundary")
    critique_parked_at = services.get_gate(critique_run.id).model_dump(mode="json", by_alias=True)[
        "parkedAt"
    ]

    answered = client.post(
        f"/api/gates/{critique_run.id}/answer",
        json={
            "parkedAt": critique_parked_at,
            "control": "request_changes",
            "answers": {},
            "note": "",
        },
    )

    assert answered.status_code == 200
    assert answered.json() == {
        "run": critique_run.id,
        "parkedAt": critique_parked_at,
        "result": "answered",
    }
    assert resume_spy == [
        {
            "id": critique_run.id,
            "action": "request_changes",
            "answers": {},
            "note": "",
        }
    ]

    contextless_note = Note.create(body="contextless gate")
    contextless_run = _park(druks_db, contextless_note)
    contextless_parked_at = services.get_gate(contextless_run.id).model_dump(
        mode="json", by_alias=True
    )["parkedAt"]

    rejected = client.post(
        f"/api/gates/{contextless_run.id}/answer",
        json={
            "parkedAt": contextless_parked_at,
            "control": "request_changes",
            "answers": {},
            "note": "",
        },
    )

    assert rejected.status_code == 400
    assert rejected.json() == {
        "code": "INVALID_GATE_ANSWER",
        "message": "request_changes needs an answer or a note to guide the re-plan",
        "retryable": False,
    }
    assert len(resume_spy) == 1


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
    assert cancelled.json() == {"run": run.id, "result": "cancelled"}

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

    empty_changes = client.post(
        f"/api/runs/{run.id}/resume",
        json={"control": "request_changes", "answers": {}, "note": ""},
    )
    assert empty_changes.status_code == 422
    assert empty_changes.json() == {
        "error": "HTTP_422",
        "detail": "request_changes needs an answer or a note to guide the re-plan",
    }
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

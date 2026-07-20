import ast
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from conftest import (
    configure_app_for_test,
    finish_agent_run,
    make_settings,
    make_test_work_item,
    seed_agent_run,
    seed_build_run,
)
from druks.accounts.models import Account, PersonalAccessToken
from druks.api.app import mcp_app
from druks.durable.models import Artifact, Run
from druks.usage.models import UsageScrape
from fastapi.testclient import TestClient
from fastmcp.client import Client
from fastmcp.client.transports import StreamableHttpTransport

_IN_APP_ASK = {
    "presentation": "in_app",
    "controls": ["approve", "request_changes", "cancel"],
    "questions": [],
}

_INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "probe", "version": "0"},
    },
}
_WIRE_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}

# tools/list order is the gateway router's declaration order.
_TOOL_NAMES = ["get_gate", "answer_gate", "get_agent_call", "cancel_run", "get_usage"]


@pytest.fixture
def app(tmp_path, db_session, monkeypatch):
    monkeypatch.setenv("DRUKS_DATA_DIR", str(tmp_path))
    return configure_app_for_test(settings=make_settings(tmp_path), authenticated=False)


@asynccontextmanager
async def live(app):
    # pytest-asyncio runs fixture setup and the test body in separate tasks,
    # which strands the endpoint's anyio task group — each test enters it.
    async with app.router.lifespan_context(app):
        yield app


@pytest.fixture
def account(db_session):
    return Account.get_or_create("op@example.com")


@pytest.fixture
def pat_token(account):
    _, token = PersonalAccessToken.create(account_id=account.id, name="agent")
    return token


@pytest.fixture
def resume_spy(monkeypatch):
    calls = []

    async def _spy(self, **fields):
        calls.append({"id": self.id, **fields})

    monkeypatch.setattr(Run, "resume", _spy)
    return calls


def _client(app, token: str) -> Client:
    def factory(**kwargs):
        kwargs.pop("verify", None)  # meaningless for the in-process transport
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://druks.test", **kwargs
        )

    transport = StreamableHttpTransport(
        "http://druks.test/mcp", auth=token, httpx_client_factory=factory
    )
    return Client(transport)


async def _call_error(client: Client, name: str, arguments: dict) -> dict:
    # The route's sanitized taxonomy dict rides after " - " in the error text.
    result = await client.call_tool(name, arguments, raise_on_error=False)
    assert result.is_error
    text = result.content[0].text
    _, _, embedded = text.partition(" - ")
    assert embedded, text
    return ast.literal_eval(embedded)


def _wire_size(structured: dict) -> int:
    return len(json.dumps(structured, separators=(",", ":"), default=str).encode())


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


async def test_mcp_rejects_missing_and_dead_tokens(app, account, db_session):
    row, token = PersonalAccessToken.create(account_id=account.id, name="agent")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://druks.test"
    ) as wire:
        anonymous = await wire.post("/mcp", json=_INIT, headers=_WIRE_HEADERS)
        assert anonymous.status_code == 401
        assert anonymous.headers["www-authenticate"].startswith("Bearer")

        garbage = await wire.post(
            "/mcp",
            json=_INIT,
            headers={**_WIRE_HEADERS, "Authorization": "Bearer druks_pat_bogus_bogus"},
        )
        assert garbage.status_code == 401
        assert 'error="invalid_token"' in garbage.headers["www-authenticate"]

        bearer = {**_WIRE_HEADERS, "Authorization": f"Bearer {token}"}
        row.expires_at = datetime.now(UTC) - timedelta(days=1)
        db_session.flush()
        expired = await wire.post("/mcp", json=_INIT, headers=bearer)
        assert expired.status_code == 401

        row.expires_at = datetime.now(UTC) + timedelta(days=1)
        row.revoke()
        revoked = await wire.post("/mcp", json=_INIT, headers=bearer)
        assert revoked.status_code == 401


async def test_mcp_subpaths_never_reach_the_spa(app):
    # An unowned /mcp/* subpath must answer JSON 404, never the SPA's HTML.
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://druks.test"
    ) as wire:
        for method in ("GET", "POST"):
            stray = await wire.request(method, "/mcp/child")
            assert stray.status_code == 404
            assert stray.headers["content-type"].startswith("application/json")


async def test_tools_list_pins_the_five_derived_from_agent_routes(app, pat_token):
    async with live(app), _client(app, pat_token) as client:
        assert "parkedAt" in (client.initialize_result.instructions or "")
        tools = {tool.name: tool for tool in await client.list_tools()}

    assert list(tools) == _TOOL_NAMES
    read_only = {"get_gate", "get_agent_call", "get_usage"}
    for name, tool in tools.items():
        annotations = tool.annotations
        assert annotations.readOnlyHint == (name in read_only)
        assert annotations.destructiveHint == (None if name in read_only else name == "cancel_run")
        assert annotations.idempotentHint == (None if name in read_only else True)
        assert tool.description, name

    # Derived schemas keep the routes' own shapes and constraints.
    assert tools["answer_gate"].inputSchema["required"] == ["run_id", "parkedAt", "control"]
    reason = tools["cancel_run"].inputSchema["properties"]["reason"]
    assert (reason["minLength"], reason["maxLength"]) == (1, 500)
    assert not tools["get_usage"].inputSchema.get("required")


async def test_claims_resolve_the_calling_account(app, db_session):
    # get_usage must answer as the token's account — the forwarded bearer.
    mine = Account.get_or_create("op@example.com")
    theirs = Account.get_or_create("peer@example.com")
    _, my_token = PersonalAccessToken.create(account_id=mine.id, name="mine")
    _, their_token = PersonalAccessToken.create(account_id=theirs.id, name="theirs")
    db_session.add(
        UsageScrape(
            harness="codex",
            account_id=mine.id,
            scraped_at=datetime.now(UTC),
            five_hour_percent_left=42,
        )
    )
    db_session.flush()

    async with live(app), _client(app, my_token) as client:
        usage = (await client.call_tool("get_usage", {})).structured_content
    codex = next(h for h in usage["harnesses"] if h["name"] == "codex")
    assert codex["fiveHourPercentLeft"] == 42

    async with live(app), _client(app, their_token) as client:
        usage = (await client.call_tool("get_usage", {})).structured_content
    codex = next(h for h in usage["harnesses"] if h["name"] == "codex")
    assert codex["fiveHourPercentLeft"] is None


async def test_gate_cycle_reads_answers_and_reports_stale_rounds(
    app, pat_token, db_session, resume_spy
):
    item = make_test_work_item(repo="o/r", title="t")
    run = _park(db_session, item.id)

    async with live(app), _client(app, pat_token) as client:
        gate = (await client.call_tool("get_gate", {"run_id": run.id})).structured_content
        assert gate["runId"] == run.id
        assert gate["ask"]["controls"] == ["approve", "request_changes", "cancel"]

        stale = await _call_error(
            client,
            "answer_gate",
            {"run_id": run.id, "parkedAt": "2020-01-01T00:00:00+00:00", "control": "approve"},
        )
        assert stale["code"] == "GATE_ROUND_STALE"
        assert stale["retryable"] is True

        answered = (
            await client.call_tool(
                "answer_gate",
                {"run_id": run.id, "parkedAt": gate["parkedAt"], "control": "approve"},
            )
        ).structured_content
        assert answered["result"] == "answered"
        assert resume_spy == [{"id": run.id, "action": "approve", "answers": {}, "note": ""}]

        run.answer_parked_at = run.input_requested_at
        db_session.flush()
        repeat = (
            await client.call_tool(
                "answer_gate",
                {"run_id": run.id, "parkedAt": gate["parkedAt"], "control": "approve"},
            )
        ).structured_content
        assert repeat["result"] == "already_answered"

        missing = await _call_error(
            client,
            "answer_gate",
            {"run_id": "no-such-run", "parkedAt": gate["parkedAt"], "control": "approve"},
        )
        assert missing["code"] == "RUN_NOT_FOUND"

        external_item = make_test_work_item(repo="o/external", title="t")
        external = seed_build_run(
            db_session,
            work_item_id=external_item.id,
            state="pending_input",
            input_gate="review",
            input_request={"presentation": "external"},
        )
        external.input_requested_at = datetime.now(UTC)
        db_session.flush()
        unanswerable = await _call_error(client, "get_gate", {"run_id": external.id})
    assert unanswerable["code"] == "GATE_NOT_ANSWERABLE"


async def test_get_agent_call_serves_bounded_tails(app, pat_token, db_session):
    call = seed_agent_run()
    call_dir = call.call_dir
    call_dir.mkdir(parents=True, exist_ok=True)
    (call_dir / "stdout.jsonl").write_bytes(b"s" * 20480)
    (call_dir / "stderr.log").write_bytes(b"e" * 10240)
    finish_agent_run(call, last_error="boom " * 100)
    Artifact.record(
        call_dir=call_dir, call_id=call.id, kind="markdown", title="Out", content="a" * 10240
    )

    async with live(app), _client(app, pat_token) as client:
        detail = (await client.call_tool("get_agent_call", {"call_id": call.id})).structured_content
        assert detail["runId"] == call.run_id
        assert len(detail["transcript"].encode()) <= 8 * 1024
        assert len(detail["stderr"].encode()) <= 4 * 1024
        assert len(detail["artifact"]["content"].encode()) <= 4 * 1024
        assert _wire_size(detail) <= 20 * 1024

        error = await _call_error(client, "get_agent_call", {"call_id": "no-such-call"})
    assert error["code"] == "AGENT_CALL_NOT_FOUND"


async def test_cancel_run_is_destructive_but_repeatable(app, pat_token, db_session, monkeypatch):
    cancels = []

    async def _spy(self, *, failure):
        cancels.append({"id": self.id, "failure": failure})

    monkeypatch.setattr(Run, "cancel", _spy)
    item = make_test_work_item(repo="o/r", title="t")
    active = seed_build_run(db_session, work_item_id=item.id, state="running")
    done_item = make_test_work_item(repo="o/done", title="t")
    done = seed_build_run(db_session, work_item_id=done_item.id, state="finished")
    gone_item = make_test_work_item(repo="o/gone", title="t")
    gone = seed_build_run(db_session, work_item_id=gone_item.id, state="cancelled")

    async with live(app), _client(app, pat_token) as client:
        cancelled = (
            await client.call_tool("cancel_run", {"run_id": active.id, "reason": "wrong branch"})
        ).structured_content
        assert cancelled == {"runId": active.id, "result": "cancelled"}
        assert cancels == [{"id": active.id, "failure": "wrong branch"}]

        repeat = (
            await client.call_tool("cancel_run", {"run_id": gone.id, "reason": "again"})
        ).structured_content
        assert repeat["result"] == "already_cancelled"

        terminal = await _call_error(
            client, "cancel_run", {"run_id": done.id, "reason": "too late"}
        )
        assert terminal["code"] == "RUN_NOT_ACTIVE"

        # A blank reason dies at route validation (422), before the service.
        blank = await client.call_tool(
            "cancel_run", {"run_id": active.id, "reason": ""}, raise_on_error=False
        )
    assert blank.is_error
    assert cancels == [{"id": active.id, "failure": "wrong branch"}]


async def test_get_usage_reads_within_budget(app, pat_token):
    async with live(app), _client(app, pat_token) as client:
        usage = (await client.call_tool("get_usage", {})).structured_content
    assert usage["runsToday"] == 0
    assert {h["name"] for h in usage["harnesses"]} >= {"claude"}
    assert _wire_size(usage) <= 4 * 1024


def test_lifespan_composes_the_endpoint_once(app, monkeypatch):
    entered = []
    original = mcp_app.router.lifespan_context

    @asynccontextmanager
    async def counting(scope_app):
        entered.append(1)
        async with original(scope_app):
            yield

    monkeypatch.setattr(mcp_app.router, "lifespan_context", counting)
    with TestClient(app) as client:
        assert client.get("/api/system/health").status_code == 200
    assert entered == [1]


def test_mcp_server_registry_routes_stay_untouched(tmp_path, db_session, monkeypatch):
    monkeypatch.setenv("DRUKS_DATA_DIR", str(tmp_path))
    app = configure_app_for_test(settings=make_settings(tmp_path))
    with TestClient(app) as client:
        listed = client.get("/api/mcp-servers")
    assert listed.status_code == 200
    # The inbound endpoint never joins the outbound server registry.
    assert "druks" not in {server["name"] for server in listed.json()}

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from conftest import make_test_work_item, seed_build_run, seed_call
from druks.accounts.models import Account
from druks.build import agent as build_agent
from druks.build.exceptions import InvalidCursor, WorkItemNotFound
from druks.build.workflows import BuildWorkflow
from druks.durable import agent as durable_agent
from druks.durable.exceptions import (
    AgentCallNotFound,
    GateNotAnswerable,
    GateNotOpen,
    GateRoundStale,
    InvalidGateAnswer,
    RunNotActive,
    RunNotFound,
)
from druks.durable.models import Artifact, Run
from druks.durable.reads import read_slice
from druks.usage import agent as usage_agent
from druks.usage.models import UsageScrape

pytestmark = pytest.mark.usefixtures("_data_dir")


@pytest.fixture
def _data_dir(tmp_path, monkeypatch):
    # AgentCall.call_dir derives from load_settings().artifacts_dir.
    monkeypatch.setenv("DRUKS_DATA_DIR", str(tmp_path))


@pytest.fixture
def account(db_session):
    return Account.get_or_create("op@example.com")


@pytest.fixture
def resume_spy(monkeypatch):
    calls = []

    async def _spy(self, **fields):
        calls.append({"id": self.id, **fields})

    monkeypatch.setattr(Run, "resume", _spy)
    return calls


def _in_app_ask(questions=()):
    return {
        "presentation": "in_app",
        "controls": ["approve", "request_changes", "cancel"],
        "questions": list(questions),
    }


def _park(db_session, item_id, *, ask=None):
    run = seed_build_run(
        db_session,
        work_item_id=item_id,
        state="pending_input",
        input_gate="review",
        input_request=ask if ask is not None else _in_app_ask(),
    )
    run.input_requested_at = datetime.now(UTC)
    db_session.flush()
    return run


# ---- read_slice -----------------------------------------------------------


def test_read_slice_tail_snaps_a_split_character(tmp_path: Path):
    path = tmp_path / "log.txt"
    path.write_bytes(b"abcd" + "é".encode() + b"wxyz")

    piece = read_slice(path, offset=-5, limit=5)

    # Byte 5 is the é's continuation byte; the slice starts after it.
    assert piece.offset == 6
    assert piece.text == "wxyz"
    assert piece.has_earlier is True
    assert piece.eof is True


def test_read_slice_head_trims_a_trailing_partial_character(tmp_path: Path):
    path = tmp_path / "log.txt"
    path.write_bytes(b"abcd" + "é".encode() + b"wxyz")

    head = read_slice(path, offset=0, limit=5)
    assert head.text == "abcd"
    assert head.next_offset == 4
    assert head.eof is False
    assert head.has_earlier is False

    rest = read_slice(path, offset=head.next_offset, limit=100)
    assert rest.text == "éwxyz"
    assert rest.eof is True


def test_read_slice_trims_a_partial_four_byte_character(tmp_path: Path):
    path = tmp_path / "log.txt"
    payload = "ab🎉cd".encode()
    path.write_bytes(payload)

    piece = read_slice(path, offset=0, limit=4)  # cuts the emoji after 2 of 4 bytes

    assert piece.text == "ab"
    assert piece.next_offset == 2


def test_read_slice_tiny_limit_still_progresses(tmp_path: Path):
    path = tmp_path / "log.txt"
    path.write_bytes("🎉x".encode())

    # A limit smaller than one character can never advance; the floor of one
    # whole character keeps pagination moving.
    piece = read_slice(path, offset=0, limit=1)
    assert piece.text == "🎉"
    assert piece.next_offset == 4

    rest = read_slice(path, offset=piece.next_offset, limit=1)
    assert rest.text == "x"
    assert rest.eof is True


def test_read_slice_invalid_bytes_still_progress(tmp_path: Path):
    # Raw terminal output isn't guaranteed UTF-8: an invalid byte can never
    # complete into a character, so it must be replaced and passed, not
    # trimmed forever.
    path = tmp_path / "log.txt"
    path.write_bytes(b"ok\xff")

    piece = read_slice(path, offset=0, limit=100)
    assert piece.text == "ok�"
    assert piece.next_offset == 3
    assert piece.eof is True

    # A surrogate prefix (ED A0) has a valid lead but can never complete;
    # trimming it would stall the reader at offset 2 forever.
    path.write_bytes(b"ok\xed\xa0")
    stuck = read_slice(path, offset=0, limit=100)
    assert stuck.text[:2] == "ok"
    assert stuck.next_offset == 4
    assert stuck.eof is True


def test_read_slice_live_tail_re_covers_a_mid_write_character(tmp_path: Path):
    path = tmp_path / "live.txt"
    path.write_bytes(b"ab" + "🎉".encode()[:2])  # the writer is mid-emoji

    piece = read_slice(path, offset=0, limit=100)
    assert piece.text == "ab"
    assert piece.next_offset == 2
    assert piece.eof is False  # the character isn't whole yet

    path.write_bytes(b"ab" + "🎉".encode())
    rest = read_slice(path, offset=piece.next_offset, limit=100)
    assert rest.text == "🎉"
    assert rest.eof is True


def test_read_slice_missing_file_is_an_empty_eof(tmp_path: Path):
    piece = read_slice(tmp_path / "absent.txt", offset=-1024, limit=1024)
    assert piece.text == ""
    assert piece.eof is True
    assert piece.has_earlier is False


# ---- gates ----------------------------------------------------------------


def test_get_gate_returns_ask_schema_and_parked_at(db_session):
    item = make_test_work_item(repo="o/r", title="t")
    question = {"id": "q1", "prompt": "Which db?", "options": [{"id": "pg", "label": "Postgres"}]}
    run = _park(db_session, item.id, ask=_in_app_ask([question]))

    view = durable_agent.get_gate(run.id)

    assert view.run_id == run.id
    assert view.gate == "review"
    assert view.parked_at == run.input_requested_at
    assert view.ask["controls"] == ["approve", "request_changes", "cancel"]
    schema = view.reply_schema
    assert schema["properties"]["control"]["enum"] == ["approve", "request_changes", "cancel"]
    assert schema["properties"]["answers"]["properties"]["q1"]["description"] == "Which db?"
    assert schema["required"] == ["control"]


def test_get_gate_bounds_an_agent_authored_ask(db_session):
    item = make_test_work_item(repo="o/r", title="t")
    question = {
        "id": "q1",
        "prompt": "🦖" * 5000,
        "options": [{"id": "a", "label": "💥" * 500}],
    }
    run = _park(db_session, item.id, ask=_in_app_ask([question]))

    view = durable_agent.get_gate(run.id)

    prompt = view.ask["questions"][0]["prompt"]
    label = view.ask["questions"][0]["options"][0]["label"]
    assert len(json.dumps(prompt, ensure_ascii=False).encode()) - 2 <= 2048
    assert len(json.dumps(label, ensure_ascii=False).encode()) - 2 <= 256
    # The reply schema derives from the bounded ask, so its description is the
    # clipped prompt — one bounded view, both structures.
    assert view.reply_schema["properties"]["answers"]["properties"]["q1"]["description"] == prompt


def test_get_gate_serves_a_bounded_artifact_chunk(db_session):
    item = make_test_work_item(repo="o/r", title="t")
    run = _park(db_session, item.id)
    call = seed_call(db_session, run, "generate_plan")
    Artifact.record(
        call_dir=call.call_dir, call_id=call.id, kind="markdown", title="Plan", content="x" * 10240
    )

    view = durable_agent.get_gate(run.id)

    assert view.artifact is not None
    assert view.artifact.call_id == call.id
    assert view.artifact.title == "Plan"
    assert len(view.artifact.chunk.text.encode()) <= 4096
    assert view.artifact.chunk.eof is False


def test_get_gate_refuses_when_not_parked_or_external(db_session):
    with pytest.raises(RunNotFound):
        durable_agent.get_gate("no-such-run")

    item = make_test_work_item(repo="o/r", title="t")
    running = seed_build_run(db_session, work_item_id=item.id, state="running")
    with pytest.raises(GateNotOpen):
        durable_agent.get_gate(running.id)

    external_item = make_test_work_item(repo="o/r2", title="t")
    external = _park(
        db_session,
        external_item.id,
        ask={"presentation": "external", "label": "Answer on the ticket"},
    )
    with pytest.raises(GateNotAnswerable):
        durable_agent.get_gate(external.id)


async def test_answer_gate_resumes_through_run_resume(db_session, resume_spy):
    item = make_test_work_item(repo="o/r", title="t")
    run = _park(db_session, item.id)

    result = await durable_agent.answer_gate(
        run.id,
        parked_at=run.input_requested_at,
        control="approve",
        answers={},
        note="ship it",
    )

    assert result.result == "answered"
    assert result.parked_at == run.input_requested_at
    assert resume_spy == [{"id": run.id, "action": "approve", "answers": {}, "note": "ship it"}]


async def test_answer_gate_uses_the_receipt_for_already_answered(db_session, resume_spy):
    item = make_test_work_item(repo="o/r", title="t")
    parked_at = datetime.now(UTC)
    run = seed_build_run(db_session, work_item_id=item.id, state="running")
    run.input_requested_at = parked_at
    run.answered_parked_at = parked_at
    db_session.flush()

    result = await durable_agent.answer_gate(
        run.id, parked_at=parked_at, control="approve", answers={}, note=""
    )

    assert result.result == "already_answered"
    assert resume_spy == []


async def test_answer_gate_error_taxonomy(db_session, resume_spy):
    with pytest.raises(RunNotFound):
        await durable_agent.answer_gate(
            "no-such-run", parked_at=datetime.now(UTC), control="approve", answers={}, note=""
        )

    item = make_test_work_item(repo="o/r", title="t")
    finished = seed_build_run(db_session, work_item_id=item.id, state="finished")
    with pytest.raises(GateNotOpen):
        await durable_agent.answer_gate(
            finished.id, parked_at=datetime.now(UTC), control="approve", answers={}, note=""
        )

    parked_item = make_test_work_item(repo="o/r2", title="t")
    run = _park(db_session, parked_item.id)
    with pytest.raises(GateRoundStale):
        await durable_agent.answer_gate(
            run.id,
            parked_at=run.input_requested_at - timedelta(seconds=5),
            control="approve",
            answers={},
            note="",
        )
    with pytest.raises(InvalidGateAnswer):
        await durable_agent.answer_gate(
            run.id, parked_at=run.input_requested_at, control="merge", answers={}, note=""
        )
    with pytest.raises(InvalidGateAnswer):
        await durable_agent.answer_gate(
            run.id,
            parked_at=run.input_requested_at,
            control="approve",
            answers={},
            note="n" * 2049,
        )

    external_item = make_test_work_item(repo="o/r3", title="t")
    external = _park(
        db_session,
        external_item.id,
        ask={"presentation": "external", "label": "Answer on the ticket"},
    )
    with pytest.raises(GateNotAnswerable):
        await durable_agent.answer_gate(
            external.id,
            parked_at=external.input_requested_at,
            control="approve",
            answers={},
            note="",
        )
    assert resume_spy == []


# ---- agent calls ----------------------------------------------------------


def test_get_agent_call_serves_bounded_tails(db_session):
    from conftest import finish_agent_run, seed_agent_run

    call = seed_agent_run()
    call_dir = call.call_dir
    call_dir.mkdir(parents=True, exist_ok=True)
    (call_dir / "stdout.jsonl").write_bytes(b"s" * 20480)
    (call_dir / "stderr.log").write_bytes(b"e" * 10240)
    finish_agent_run(call, last_error="boom " * 100)
    Artifact.record(
        call_dir=call_dir, call_id=call.id, kind="markdown", title="Out", content="a" * 10240
    )

    detail = durable_agent.get_agent_call(call.id)

    assert detail.run_id == call.run_id
    assert detail.call.id == call.id
    assert len(detail.call.last_error or "") <= 160
    assert len(detail.transcript.text.encode()) <= 8192
    assert detail.transcript.offset == 20480 - 8192
    assert detail.transcript.eof is True
    assert detail.transcript.has_earlier is True
    assert len(detail.stderr.text.encode()) <= 4096
    assert detail.stderr.has_earlier is True
    assert detail.artifact is not None
    assert len(detail.artifact.chunk.text.encode()) <= 4096

    with pytest.raises(AgentCallNotFound):
        durable_agent.get_agent_call("no-such-call")


def test_get_agent_call_without_files_reads_empty(db_session):
    from conftest import seed_agent_run

    call = seed_agent_run()

    detail = durable_agent.get_agent_call(call.id)

    assert detail.transcript.text == ""
    assert detail.transcript.eof is True
    assert detail.stderr.has_earlier is False
    assert detail.artifact is None


# ---- cancel ---------------------------------------------------------------


async def test_cancel_run_paths(db_session):
    item = make_test_work_item(repo="o/r", title="t")
    run = seed_build_run(db_session, work_item_id=item.id, state="running")

    result = await durable_agent.cancel_run(run.id, reason="stuck")
    assert result.result == "cancelled"
    db_session.expire_all()
    assert Run.get(run.id).state == "cancelled"
    assert Run.get(run.id).failure == "stuck"

    again = await durable_agent.cancel_run(run.id, reason="stuck")
    assert again.result == "already_cancelled"

    finished_item = make_test_work_item(repo="o/r2", title="t")
    finished = seed_build_run(db_session, work_item_id=finished_item.id, state="finished")
    with pytest.raises(RunNotActive):
        await durable_agent.cancel_run(finished.id, reason="late")

    with pytest.raises(RunNotFound):
        await durable_agent.cancel_run("no-such-run", reason="x")


# ---- work board -----------------------------------------------------------


def test_list_work_filters(db_session, account):
    mine_item = make_test_work_item(repo="o/mine", title="mine")
    mine_run = seed_build_run(db_session, work_item_id=mine_item.id, state="running")
    mine_run.account_id = account.id
    parked_item = make_test_work_item(repo="o/parked", title="parked")
    _park(db_session, parked_item.id)
    failed_item = make_test_work_item(repo="o/failed", title="failed")
    seed_build_run(db_session, work_item_id=failed_item.id, state="failed", failure="crash")
    db_session.flush()

    assert {i.work_item_id for i in build_agent.list_work(account).items} == {
        mine_item.id,
        parked_item.id,
        failed_item.id,
    }
    mine = build_agent.list_work(account, filter="mine").items
    assert [i.work_item_id for i in mine] == [mine_item.id]
    parked = build_agent.list_work(account, filter="parked").items
    assert [i.work_item_id for i in parked] == [parked_item.id]
    assert parked[0].status.state.value == "pending_input"
    assert parked[0].status.gate == "review"
    active = build_agent.list_work(account, filter="active").items
    assert {i.work_item_id for i in active} == {mine_item.id, parked_item.id}
    failed = build_agent.list_work(account, filter="failed").items
    assert [i.work_item_id for i in failed] == [failed_item.id]
    assert failed[0].status.failure == "crash"


def test_list_work_walks_the_keyset_cursor(db_session, account):
    for index in range(20):
        make_test_work_item(repo=f"o/r{index}", title=f"item {index}")

    first = build_agent.list_work(account)
    assert len(first.items) == 12
    assert first.next_cursor
    second = build_agent.list_work(account, cursor=first.next_cursor)
    assert len(second.items) == 8
    assert second.next_cursor is None
    seen = [item.work_item_id for item in [*first.items, *second.items]]
    assert len(seen) == len(set(seen)) == 20

    with pytest.raises(InvalidCursor):
        build_agent.list_work(account, cursor="!!not-a-cursor!!")


def test_list_work_page_stays_within_budget(db_session, account):
    # The budgets bound the SERIALIZED response, so the worst-case page must
    # hold for maximally multibyte text and for control bytes that JSON
    # escaping blows up six-fold (ANSI color codes in real failures).
    for index in range(13):
        item = make_test_work_item(
            repo=f"o/very-long-repo-name-{index}",
            title="🦖" * 400,
            remote_url="https://tracker.example.com/" + "x" * 1000,
        )
        seed_build_run(db_session, work_item_id=item.id, state="failed", failure="\x1b[31m💥" * 500)

    page = build_agent.list_work(account)

    assert len(page.items) == 12
    for row in page.items:
        assert len(json.dumps(row.title, ensure_ascii=False).encode()) - 2 <= 120
        assert len(json.dumps(row.status.failure or "", ensure_ascii=False).encode()) - 2 <= 160
    assert len(page.model_dump_json(by_alias=True).encode()) <= 12 * 1024


def test_get_work_item_detail_stays_within_budget(db_session, account):
    item = make_test_work_item(
        repo="o/r",
        title="🦖" * 400,
        remote_key="ACME-1",
        remote_url="https://tracker.example.com/" + "x" * 17000,
    )
    for _ in range(6):
        run = seed_build_run(db_session, work_item_id=item.id, state="finished")
        for _ in range(6):
            seed_call(db_session, run, "implement", status="failed", last_error="💥" * 1000)
    seed_build_run(db_session, work_item_id=item.id, state="failed", failure="💥" * 1000)

    detail = build_agent.get_work_item(item.id)

    assert detail.work_item_id == item.id
    assert len(detail.title.encode()) <= 120
    assert len(detail.runs) == 5
    for run_summary in detail.runs:
        assert len(run_summary.agent_calls) <= 5
        for call_summary in run_summary.agent_calls:
            assert len((call_summary.last_error or "").encode()) <= 160
    assert detail.links.repo == "https://github.com/o/r"
    assert len(detail.model_dump_json(by_alias=True).encode()) <= 16 * 1024

    with pytest.raises(WorkItemNotFound):
        build_agent.get_work_item(999999)


# ---- dispatch -------------------------------------------------------------


@pytest.fixture
def start_stub(db_session, monkeypatch):
    from conftest import seed_run

    async def _start(cls, **kwargs):
        if not Run.get("run-dispatch"):
            seed_run(db_session, "run-dispatch")
        return "run-dispatch"

    monkeypatch.setattr(BuildWorkflow, "start", classmethod(_start))


async def test_dispatch_by_work_item_id(db_session, account, start_stub):
    item = make_test_work_item(repo="o/r", title="t")

    result = await build_agent.dispatch(account, work_item_id=item.id)

    assert result.work_item_id == item.id
    assert result.run_id == "run-dispatch"
    run_row = Run.get("run-dispatch")
    assert result.is_owned_by_caller == (run_row.account_id == account.id)
    assert item.build_run_id == "run-dispatch"
    assert item.status is None


async def test_dispatch_by_ticket_ref(db_session, account, start_stub):
    item = make_test_work_item(repo="o/r", title="t", source="linear", remote_key="ACME-7")

    result = await build_agent.dispatch(account, source="linear", ticket_ref="ACME-7")

    assert result.work_item_id == item.id
    assert result.run_id == "run-dispatch"


async def test_dispatch_stamps_ambient_attribution(db_session, account, start_stub):
    # The MCP boundary has no session gate, so the service itself must stamp
    # the caller before the launch policy runs — start() inherits it when the
    # item carries no assignee.
    from druks.accounts import sessions

    item = make_test_work_item(repo="o/r", title="t")
    token = sessions.current_account_id.set(None)
    try:
        await build_agent.dispatch(account, work_item_id=item.id)
        assert sessions.current_account_id.get() == account.id
    finally:
        sessions.current_account_id.reset(token)


async def test_dispatch_unknown_item(db_session, account, start_stub):
    with pytest.raises(WorkItemNotFound):
        await build_agent.dispatch(account, work_item_id=424242)
    with pytest.raises(WorkItemNotFound):
        await build_agent.dispatch(account, source="jira", ticket_ref="NOPE-1")


# ---- usage ----------------------------------------------------------------


def test_get_usage_is_a_bounded_pure_read(db_session, account):
    from conftest import seed_run
    from druks.durable.models import AgentCall

    now = datetime.now(UTC)
    run = seed_run(db_session, "run-usage")
    for index in range(30):
        db_session.add(
            AgentCall(
                run_id=run.id,
                account_id=account.id,
                sandbox_host_id="host",
                model="gpt-5.5",
                status="succeeded",
                finished_at=now,
                cost_usd=0.5,
                cost_metadata={"input_tokens": 100 + index},
            )
        )
    for tick in range(40):
        UsageScrape(
            harness="claude",
            account_id=account.id,
            scraped_at=now - timedelta(minutes=5 * tick),
            plan_tier="max",
            five_hour_percent_left=90 - tick,
            five_hour_resets_at=now + timedelta(hours=2),
            week_percent_left=80 - tick,
            week_resets_at=now + timedelta(days=3),
        ).save()

    usage = usage_agent.get_usage(account)

    assert usage.runs_today == 30
    assert usage.spend_today_usd == pytest.approx(15.0)
    assert usage.tokens_today == sum(100 + i for i in range(30))
    claude = next(h for h in usage.harnesses if h.name == "claude")
    assert claude.plan_tier == "max"
    assert claude.five_hour_percent_left == 90
    assert len(claude.five_hour_history) <= 8
    assert len(claude.week_history) <= 8
    # The newest sample anchors each trend.
    assert claude.week_history[-1].pct == 80
    assert len(usage.model_dump_json(by_alias=True).encode()) <= 4 * 1024


def test_get_usage_only_counts_the_callers_spend(db_session, account):
    from conftest import seed_run
    from druks.durable.models import AgentCall

    other = Account.get_or_create("other@example.com")
    run = seed_run(db_session, "run-usage-other")
    db_session.add(
        AgentCall(
            run_id=run.id,
            account_id=other.id,
            sandbox_host_id="host",
            model="gpt-5.5",
            status="succeeded",
            finished_at=datetime.now(UTC),
            cost_usd=9.0,
        )
    )
    db_session.flush()

    usage = usage_agent.get_usage(account)

    assert usage.runs_today == 0
    assert usage.spend_today_usd == 0.0

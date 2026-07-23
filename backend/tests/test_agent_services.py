from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from conftest import make_test_work_item, seed_build_run, seed_call
from druks.accounts.models import Account
from druks.durable.models import Artifact, Run
from druks.durable.reads import read_slice
from druks.mcp.gateway import exceptions, services
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


def test_read_slice_paginates_a_window(tmp_path: Path):
    path = tmp_path / "log.txt"
    path.write_bytes(b"hello world")

    head = read_slice(path, offset=0, limit=5)
    assert head.text == "hello"
    assert head.next_offset == 5
    assert head.eof is False
    assert head.has_earlier is False

    rest = read_slice(path, offset=head.next_offset, limit=100)
    assert rest.text == " world"
    assert rest.eof is True


def test_read_slice_reads_the_tail(tmp_path: Path):
    path = tmp_path / "log.txt"
    path.write_bytes(b"hello world")

    tail = read_slice(path, offset=-5, limit=5)
    assert tail.offset == 6
    assert tail.text == "world"
    assert tail.has_earlier is True
    assert tail.eof is True


def test_read_slice_missing_file_is_an_empty_eof(tmp_path: Path):
    piece = read_slice(tmp_path / "absent.txt", offset=-1024, limit=1024)
    assert piece.text == ""
    assert piece.eof is True
    assert piece.has_earlier is False


# ---- gates ----------------------------------------------------------------


def test_get_gate_returns_the_ask_and_parked_at(db_session):
    item = make_test_work_item(repo="o/r", title="t")
    question = {"id": "q1", "prompt": "Which db?", "options": [{"id": "pg", "label": "Postgres"}]}
    run = _park(db_session, item.id, ask=_in_app_ask([question]))

    view = services.get_gate(run.id)

    assert view.run_id == run.id
    assert view.gate == "review"
    assert view.parked_at == run.input_requested_at
    assert view.ask["controls"] == ["approve", "request_changes", "cancel"]
    assert view.ask["questions"][0]["prompt"] == "Which db?"


def test_get_gate_serves_the_artifact(db_session):
    item = make_test_work_item(repo="o/r", title="t")
    run = _park(db_session, item.id)
    call = seed_call(db_session, run, "generate_plan")
    Artifact.record(
        call_dir=call.call_dir, call_id=call.id, kind="markdown", title="Plan", content="x" * 10240
    )

    view = services.get_gate(run.id)

    assert view.artifact is not None
    assert view.artifact.call_id == call.id
    assert view.artifact.title == "Plan"
    assert len(view.artifact.content.encode()) <= 4096


def test_get_gate_refuses_when_not_parked_or_external(db_session):
    with pytest.raises(exceptions.RunNotFound):
        services.get_gate("no-such-run")

    item = make_test_work_item(repo="o/r", title="t")
    running = seed_build_run(db_session, work_item_id=item.id, state="running")
    with pytest.raises(exceptions.GateNotOpen):
        services.get_gate(running.id)

    external_item = make_test_work_item(repo="o/r2", title="t")
    external = _park(
        db_session,
        external_item.id,
        ask={"presentation": "external", "label": "Answer on the ticket"},
    )
    with pytest.raises(exceptions.GateNotAnswerable):
        services.get_gate(external.id)


async def test_answer_gate_resumes_through_run_resume(db_session, resume_spy):
    item = make_test_work_item(repo="o/r", title="t")
    run = _park(db_session, item.id)

    result = await services.answer_gate(
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
    run.answer_parked_at = parked_at
    db_session.flush()

    result = await services.answer_gate(
        run.id, parked_at=parked_at, control="approve", answers={}, note=""
    )

    assert result.result == "already_answered"
    assert resume_spy == []


async def test_answer_gate_error_taxonomy(db_session, resume_spy):
    with pytest.raises(exceptions.RunNotFound):
        await services.answer_gate(
            "no-such-run", parked_at=datetime.now(UTC), control="approve", answers={}, note=""
        )

    item = make_test_work_item(repo="o/r", title="t")
    finished = seed_build_run(db_session, work_item_id=item.id, state="finished")
    with pytest.raises(exceptions.GateNotOpen):
        await services.answer_gate(
            finished.id, parked_at=datetime.now(UTC), control="approve", answers={}, note=""
        )

    parked_item = make_test_work_item(repo="o/r2", title="t")
    run = _park(db_session, parked_item.id)
    with pytest.raises(exceptions.GateRoundStale):
        await services.answer_gate(
            run.id,
            parked_at=run.input_requested_at - timedelta(seconds=5),
            control="approve",
            answers={},
            note="",
        )
    with pytest.raises(exceptions.InvalidGateAnswer):
        await services.answer_gate(
            run.id, parked_at=run.input_requested_at, control="merge", answers={}, note=""
        )

    external_item = make_test_work_item(repo="o/r3", title="t")
    external = _park(
        db_session,
        external_item.id,
        ask={"presentation": "external", "label": "Answer on the ticket"},
    )
    with pytest.raises(exceptions.GateNotAnswerable):
        await services.answer_gate(
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

    detail = services.get_agent_call(call.id)

    assert detail.run_id == call.run_id
    assert detail.call.id == call.id
    assert detail.call.last_error == "boom " * 100
    assert detail.transcript == "s" * 8192
    assert detail.stderr == "e" * 4096
    assert detail.artifact is not None
    assert detail.artifact.content == "a" * 4096

    with pytest.raises(exceptions.AgentCallNotFound):
        services.get_agent_call("no-such-call")


def test_get_agent_call_without_files_reads_empty(db_session):
    from conftest import seed_agent_run

    call = seed_agent_run()

    detail = services.get_agent_call(call.id)

    assert detail.transcript == ""
    assert detail.stderr == ""
    assert detail.artifact is None


# ---- cancel ---------------------------------------------------------------


async def test_cancel_run_paths(db_session):
    item = make_test_work_item(repo="o/r", title="t")
    run = seed_build_run(db_session, work_item_id=item.id, state="running")

    result = await services.cancel_run(run.id, reason="stuck")
    assert result.result == "cancelled"
    db_session.expire_all()
    assert Run.get(run.id).state == "cancelled"
    assert Run.get(run.id).failure == "stuck"

    again = await services.cancel_run(run.id, reason="stuck")
    assert again.result == "already_cancelled"

    finished_item = make_test_work_item(repo="o/r2", title="t")
    finished = seed_build_run(db_session, work_item_id=finished_item.id, state="finished")
    with pytest.raises(exceptions.RunNotActive):
        await services.cancel_run(finished.id, reason="late")

    with pytest.raises(exceptions.RunNotFound):
        await services.cancel_run("no-such-run", reason="x")


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

    usage = services.get_usage(account)

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

    usage = services.get_usage(account)

    assert usage.runs_today == 0
    assert usage.spend_today_usd == 0.0

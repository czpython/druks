from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest
from conftest import make_test_note, seed_note_run
from druks.accounts.models import Account
from druks.api import runs
from druks.api.exceptions import RunNotActive, RunNotFailed, RunNotFound, SubjectBusy
from druks.durable.engine import run_queue
from druks.durable.enums import WorkflowEvent
from druks.durable.models import Artifact, Run
from druks.durable.reads import read_slice
from druks.mcp.gateway import exceptions, services
from druks.testing import seed_call, seed_dbos_status
from druks.usage.models import UsageScrape

pytestmark = pytest.mark.usefixtures("_data_dir")


@pytest.fixture
def _data_dir(tmp_path, monkeypatch):
    # AgentCall.call_dir derives from load_settings().artifacts_dir.
    monkeypatch.setenv("DRUKS_DATA_DIR", str(tmp_path))


@pytest.fixture
def account(druks_db):
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
        "controls": ["approve", "request_changes"],
        "questions": list(questions),
    }


def _park(druks_db, note, *, ask=None):
    run = seed_note_run(
        druks_db,
        note=note,
        state="parked",
        input_gate="review",
        input_request=ask if ask is not None else _in_app_ask(),
    )
    run.input_requested_at = datetime.now(UTC)
    druks_db.flush()
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


def test_get_gate_returns_the_ask_and_parked_at(druks_db):
    item = make_test_note()
    question = {"id": "q1", "prompt": "Which db?", "options": [{"id": "pg", "label": "Postgres"}]}
    run = _park(druks_db, item, ask=_in_app_ask([question]))

    view = services.get_gate(run.id)

    assert view.run == run.id
    assert view.gate == "review"
    assert view.parked_at == run.input_requested_at
    assert view.ask["controls"] == ["approve", "request_changes"]
    assert view.ask["questions"][0]["prompt"] == "Which db?"


def test_get_gate_serves_the_artifact(druks_db):
    item = make_test_note()
    run = _park(druks_db, item)
    call = seed_call(druks_db, run, "generate_plan")
    Artifact.record(
        call_dir=call.call_dir, call_id=call.id, kind="markdown", title="Plan", content="x" * 10240
    )

    view = services.get_gate(run.id)

    assert view.artifact is not None
    assert view.artifact.call_id == call.id
    assert view.artifact.title == "Plan"
    assert len(view.artifact.content.encode()) <= 4096


def test_get_gate_refuses_when_not_parked_or_external(druks_db):
    with pytest.raises(RunNotFound):
        services.get_gate("no-such-run")

    item = make_test_note()
    running = seed_note_run(druks_db, note=item, state="running")
    with pytest.raises(exceptions.GateNotOpen):
        services.get_gate(running.id)

    external_item = make_test_note()
    external = _park(
        druks_db,
        external_item,
        ask={"presentation": "external", "label": "Answer on the ticket"},
    )
    with pytest.raises(exceptions.GateNotAnswerable):
        services.get_gate(external.id)


# The answered and already-answered happy paths are pinned at both doors —
# test_agent_routes (REST) and test_mcp_endpoint (MCP); this file keeps the
# taxonomy arms those tests don't reach.


async def test_answer_gate_error_taxonomy(druks_db, resume_spy):
    with pytest.raises(RunNotFound):
        await services.answer_gate(
            "no-such-run", parked_at=datetime.now(UTC), control="approve", answers={}, note=""
        )

    item = make_test_note()
    finished = seed_note_run(druks_db, note=item, state="finished")
    with pytest.raises(exceptions.GateNotOpen):
        await services.answer_gate(
            finished.id, parked_at=datetime.now(UTC), control="approve", answers={}, note=""
        )

    parked_item = make_test_note()
    run = _park(druks_db, parked_item)
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

    external_item = make_test_note()
    external = _park(
        druks_db,
        external_item,
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


def test_get_agent_call_serves_bounded_tails(druks_db):
    from conftest import finish_agent_run, seed_note_agent_run

    call = seed_note_agent_run()
    call_dir = call.call_dir
    call_dir.mkdir(parents=True, exist_ok=True)
    (call_dir / "stdout.jsonl").write_bytes(b"s" * 20480)
    (call_dir / "stderr.log").write_bytes(b"e" * 10240)
    finish_agent_run(call, last_error="boom " * 100)
    Artifact.record(
        call_dir=call_dir, call_id=call.id, kind="markdown", title="Out", content="a" * 10240
    )

    detail = services.get_agent_call(call.id)

    assert detail.run == call.run_id
    assert detail.call.id == call.id
    assert detail.call.last_error == "boom " * 100
    assert detail.transcript == "s" * 8192
    assert detail.stderr == "e" * 4096
    assert detail.artifact is not None
    assert detail.artifact.content == "a" * 4096

    with pytest.raises(exceptions.AgentCallNotFound):
        services.get_agent_call("no-such-call")


def test_get_agent_call_without_files_reads_empty(druks_db):
    from conftest import seed_note_agent_run

    call = seed_note_agent_run()

    detail = services.get_agent_call(call.id)

    assert detail.transcript == ""
    assert detail.stderr == ""
    assert detail.artifact is None


# ---- cancel ---------------------------------------------------------------


async def test_cancel_run_paths(druks_db):
    item = make_test_note()
    run = seed_note_run(druks_db, note=item, state="running")

    result = await runs.cancel_run(run.id, reason="stuck")
    assert result.result == "cancelled"
    druks_db.expire_all()
    assert Run.get(run.id).state == "cancelled"
    assert Run.get(run.id).failure == "stuck"

    again = await runs.cancel_run(run.id, reason="stuck")
    assert again.result == "already_cancelled"

    finished_item = make_test_note()
    finished = seed_note_run(druks_db, note=finished_item, state="finished")
    with pytest.raises(RunNotActive):
        await runs.cancel_run(finished.id, reason="late")

    with pytest.raises(RunNotFound):
        await runs.cancel_run("no-such-run", reason="x")


async def test_run_retry_forks_from_the_failed_step(druks_db, monkeypatch):
    item = make_test_note()
    run = seed_note_run(druks_db, note=item, state="failed")
    retried_run_id = "retried-run"
    steps = [
        {"function_id": 2, "error": None},
        {"function_id": 6, "error": RuntimeError("first failure")},
        {"function_id": 8, "error": RuntimeError("final failure")},
    ]
    list_steps = mock.AsyncMock(return_value=steps)
    events = []

    async def _fork(workflow_id, start_step, *, queue_name):
        seed_dbos_status(
            druks_db,
            retried_run_id,
            "scheduled",
            subject=item.identity,
        )
        return SimpleNamespace(workflow_id=retried_run_id)

    async def _publish(event, **facts):
        events.append((event, facts))

    fork = mock.AsyncMock(side_effect=_fork)
    monkeypatch.setattr("dbos.DBOS.list_workflow_steps_async", list_steps)
    monkeypatch.setattr("dbos.DBOS.fork_workflow_async", fork)
    monkeypatch.setattr("druks.durable.models.publish", _publish)

    result = await run.retry()

    assert result == retried_run_id
    list_steps.assert_awaited_once_with(run.id)
    fork.assert_awaited_once_with(run.id, 8, queue_name=run_queue.name)
    druks_db.expire_all()
    retried = Run.get(retried_run_id)
    assert retried.kind == run.kind
    assert retried.account_id == run.account_id
    assert retried.state == "scheduled"
    assert events == [
        (
            WorkflowEvent.RETRIED,
            # Subject ids ride the DBOS attributes as strings — the one shape
            # every reader compares.
            {
                "subject": {"type": "note", "id": str(item.id)},
                "kind": run.kind,
                "run": retried_run_id,
            },
        )
    ]


async def test_run_retry_restarts_at_step_one_without_a_failed_checkpoint(druks_db, monkeypatch):
    run = seed_note_run(druks_db, state="failed")
    list_steps = mock.AsyncMock(return_value=[{"function_id": 2, "error": None}])
    fork = mock.AsyncMock(return_value=SimpleNamespace(workflow_id="clean-retry"))
    monkeypatch.setattr("dbos.DBOS.list_workflow_steps_async", list_steps)
    monkeypatch.setattr("dbos.DBOS.fork_workflow_async", fork)
    monkeypatch.setattr("druks.durable.models.publish", mock.AsyncMock())

    await run.retry()

    fork.assert_awaited_once_with(run.id, 1, queue_name=run_queue.name)


async def test_retry_run_refuses_a_non_failed_run(druks_db, monkeypatch):
    run = seed_note_run(druks_db, state="finished")
    retry = mock.AsyncMock()
    monkeypatch.setattr(Run, "retry", retry)

    with pytest.raises(RunNotFailed) as error:
        await runs.retry_run(run.id)

    assert error.value.code == "RUN_NOT_FAILED"
    assert error.value.retryable is False
    retry.assert_not_awaited()


async def test_retry_run_refuses_a_busy_subject(druks_db, monkeypatch):
    item = make_test_note()
    failed = seed_note_run(druks_db, note=item, state="failed")
    active = seed_note_run(druks_db, note=item, state="running")
    retry = mock.AsyncMock()
    monkeypatch.setattr(Run, "retry", retry)

    with pytest.raises(SubjectBusy) as error:
        await runs.retry_run(failed.id)

    assert str(error.value) == f"The subject already has active run {active.id}."
    assert error.value.retryable is True
    retry.assert_not_awaited()


async def test_retry_run_retries_a_failed_run(druks_db, monkeypatch):
    run = seed_note_run(druks_db, state="failed")
    retry = mock.AsyncMock(return_value="retried-run")
    monkeypatch.setattr(Run, "retry", retry)

    result = await runs.retry_run(run.id)

    assert result.run == "retried-run"
    retry.assert_awaited_once_with()


async def test_retry_run_refuses_a_missing_run(druks_db):
    with pytest.raises(RunNotFound) as error:
        await runs.retry_run("no-such-run")

    assert error.value.code == "RUN_NOT_FOUND"
    assert error.value.retryable is False


# ---- usage ----------------------------------------------------------------


def test_get_usage_is_a_bounded_pure_read(druks_db, account):
    from druks.durable.models import AgentCall
    from druks.testing import seed_run
    from druks_field_notes.workflows import Summarize

    now = datetime.now(UTC)
    run = seed_run(druks_db, kind=Summarize.kind, run_id="run-usage")
    for index in range(30):
        druks_db.add(
            AgentCall(
                run_id=run.id,
                agent="summarize",
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
            weeks=[
                {
                    "percent_left": 80 - tick,
                    "resets_at": (now + timedelta(days=3)).isoformat(),
                    "model": None,
                },
                {
                    "percent_left": 40 - tick,
                    "resets_at": (now + timedelta(days=2)).isoformat(),
                    "model": "Fable",
                },
            ],
        ).save()

    usage = services.get_usage(account)

    assert usage.runs_today == 30
    assert usage.spend_today_usd == pytest.approx(15.0)
    assert usage.tokens_today == sum(100 + i for i in range(30))
    claude = next(h for h in usage.harnesses if h.name == "claude")
    assert claude.plan_tier == "max"
    assert claude.five_hour_percent_left == 90
    assert claude.week_percent_left == 40
    assert claude.week_resets_at == now + timedelta(days=2)
    assert len(claude.five_hour_history) <= 8
    assert len(claude.week_history) <= 8
    # The newest sample anchors each trend.
    assert claude.week_history[-1].pct == 40
    assert len(usage.model_dump_json(by_alias=True).encode()) <= 4 * 1024


def test_get_usage_only_counts_the_callers_spend(druks_db, account):
    from druks.durable.models import AgentCall
    from druks.testing import seed_run
    from druks_field_notes.workflows import Summarize

    other = Account.get_or_create("other@example.com")
    run = seed_run(druks_db, kind=Summarize.kind, run_id="run-usage-other")
    druks_db.add(
        AgentCall(
            run_id=run.id,
            agent="summarize",
            account_id=other.id,
            sandbox_host_id="host",
            model="gpt-5.5",
            status="succeeded",
            finished_at=datetime.now(UTC),
            cost_usd=9.0,
        )
    )
    druks_db.flush()

    usage = services.get_usage(account)

    assert usage.runs_today == 0
    assert usage.spend_today_usd == 0.0

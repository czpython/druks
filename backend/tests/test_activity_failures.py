import json
from unittest.mock import AsyncMock

import pytest
from dbos import DBOS
from druks.agents import Agent, AgentOutput
from druks.durable.models import Run
from druks.events.models import Event
from druks.events.routes import list_feed
from druks.harnesses.codex import CodexHarness
from druks.harnesses.exceptions import HarnessSpendLimitError, Retry
from druks.sandbox.datastructures import HarnessRunResult
from druks.testing import seed_run
from druks.usage.models import UsageScrape
from druks.workflows import _execute_run, current_workflow
from druks_field_notes.models import Note
from druks_field_notes.workflows import Summarize
from sqlalchemy import select

SPEND_CAP_MESSAGE = (
    "You hit your spend cap set by the owner of your workspace. "
    "Ask an owner to increase your spend cap to continue."
)
FAILURE_PROBE = Agent(id="activity_failure_probe", contract=AgentOutput)


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_codex_spend_limit_preserves_the_terminal_message(stream):
    result = HarnessRunResult(
        returncode=1,
        stdout=json.dumps({"type": "error", "message": SPEND_CAP_MESSAGE}).encode()
        if stream == "stdout"
        else b"",
        stderr=SPEND_CAP_MESSAGE.encode() if stream == "stderr" else b"",
    )

    with pytest.raises(HarnessSpendLimitError) as raised:
        CodexHarness.check_returncode(result)

    assert raised.value.code == "spend_limit"
    assert raised.value.retry is Retry.NEVER
    assert str(raised.value) == f"codex exited with 1. {SPEND_CAP_MESSAGE}"


@pytest.fixture
def inline_checkpoints(monkeypatch):
    async def run_step(options, operation):
        return await operation()

    sleep = AsyncMock()
    monkeypatch.setattr(DBOS, "run_step_async", run_step)
    monkeypatch.setattr(DBOS, "sleep_async", sleep)
    return sleep


async def test_spend_limit_records_one_terminal_attempt_without_quota_wait(
    druks_db, monkeypatch, inline_checkpoints
):
    note = await Note.create(body="An unpaid observation")
    run = await seed_run(druks_db, kind=Summarize.kind, subject=note)
    workflow = Summarize()
    workflow._workflow_id = run.id
    workflow.account_id = run.account_id
    quota = AsyncMock()
    monkeypatch.setattr(UsageScrape, "latest_for", quota)
    attempts = 0

    async def run_agent(self, session, **context):
        nonlocal attempts
        attempts += 1
        CodexHarness.check_returncode(
            HarnessRunResult(returncode=1, stdout=b"", stderr=SPEND_CAP_MESSAGE.encode())
        )

    monkeypatch.setattr(Agent, "_run", run_agent)
    token = current_workflow.set(workflow)
    try:
        with pytest.raises(HarnessSpendLimitError):
            await _execute_run(run.id, run.kind, note.identity, run.account_id, FAILURE_PROBE)
    finally:
        current_workflow.reset(token)

    assert attempts == 1
    inline_checkpoints.assert_not_awaited()
    quota.assert_not_awaited()
    druks_db.expunge_all()
    recorded_run = await druks_db.get(Run, run.id)
    assert recorded_run.failure_code == "spend_limit"
    events = list(await druks_db.scalars(select(Event).filter_by(type="workflow.failed")))
    assert len(events) == 1
    assert events[0].payload == {
        "run": run.id,
        "kind": Summarize.kind,
        "failure": f"codex exited with 1. {SPEND_CAP_MESSAGE}",
        "failure_code": "spend_limit",
    }


async def test_unclassified_application_failure_keeps_its_reason_and_run(
    druks_db, inline_checkpoints
):
    note = await Note.create(body="A failed observation")
    run = await seed_run(druks_db, kind=Summarize.kind, subject=note)

    async def fail():
        raise RuntimeError("The source document is missing")

    with pytest.raises(RuntimeError, match="source document"):
        await _execute_run(run.id, run.kind, note.identity, run.account_id, fail)

    feed = await list_feed(session=druks_db, topic="workflow.failed")
    assert len(feed.items) == 1
    assert feed.items[0].payload == {
        "run": run.id,
        "kind": Summarize.kind,
        "failure": "The source document is missing",
    }

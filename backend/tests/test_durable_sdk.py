import asyncio
import contextlib
import inspect
import os
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import psycopg
import pytest
from dbos import DBOS
from druks.accounts.context import current_account_id
from druks.accounts.models import Account
from druks.agents import Agent, AgentOutput
from druks.apps.loader import register_workflow_package
from druks.apps.registry import Registry, agents, workflows
from druks.browser.enums import BrowserSessionPayloadFormat, BrowserSessionStatus
from druks.browser.exceptions import BrowserSessionSignedOutError
from druks.browser.models import StoredBrowserSession
from druks.database import configure_session, get_session, session_scope
from druks.db import db_session
from druks.durable import FatalError, Run, RunState, WorkflowError, engine
from druks.durable.dbos_state import latest_invocations, workflow_status
from druks.durable.engine import (
    _scheduled,
    apply_schedules,
    configure_engine,
    init_dbos,
    launch,
    shutdown,
    trigger_schedule,
)
from druks.durable.enums import AgentCallStatus, WorkflowEvent
from druks.durable.models import AgentCall, Artifact
from druks.events.models import Event
from druks.exceptions import SessionNotBoundError
from druks.models import StoredSubject
from druks.sandbox.datastructures import AgentResult
from druks.secrets.datastructures import Audience
from druks.secrets.enums import SecretKind
from druks.secrets.models import VaultSecret
from druks.signals import subscribe
from druks.testing import init_db
from druks.user_settings.models import InstallationSettings, SettingsOverride
from druks.workflows import Gate, OperatorReply, Subject, Workflow, step, task
from druks_field_notes.models import Note
from druks_field_notes.workflows import Summarize
from pydantic import ValidationError
from sqlalchemy import NullPool, create_engine, delete, select, text
from sqlalchemy.ext.asyncio import create_async_engine

PG_BASE = os.environ.get("DRUKS_TEST_PG", "postgresql://druks:druks@localhost:5432")
DB = "druks_durable_test"
URL = f"{PG_BASE.replace('postgresql://', 'postgresql+psycopg://')}/{DB}"


try:
    psycopg.connect(f"{PG_BASE}/postgres", connect_timeout=2).close()
except psycopg.Error:
    POSTGRES_AVAILABLE = False
else:
    POSTGRES_AVAILABLE = True


pytestmark = [
    pytest.mark.skipif(not POSTGRES_AVAILABLE, reason="test Postgres not reachable"),
    pytest.mark.asyncio(loop_scope="module"),
]


class Decision(AgentOutput):
    # DBOS must be able to pickle this step result.
    action: str


class ReviewResult(AgentOutput):
    action: str

    def to_artifact(self) -> dict[str, str]:
        return {"kind": "markdown", "title": "Review", "content": self.action}

    def to_event(self) -> dict[str, str]:
        return {"topic": "review.completed", "summary": self.action}


SINK: list[str] = []
TASK_RETRY_ATTEMPTS = 0
STEP_RETRY_ATTEMPTS = 0


class Widget(StoredSubject):
    __tablename__ = "test_widgets"

    def get_key(self) -> str:
        return f"W-{self.id}"


class Gadget(Subject):
    pass


@pytest.fixture(scope="module", autouse=True)
async def runtime():
    original_database_url = os.environ.get("DRUKS_DATABASE_URL")

    admin = psycopg.connect(f"{PG_BASE}/postgres", autocommit=True)
    admin.execute(f"DROP DATABASE IF EXISTS {DB}")
    admin.execute(f"CREATE DATABASE {DB}")
    admin.close()

    schema_engine = create_engine(URL)
    init_db(schema_engine)
    engine = create_async_engine(URL, poolclass=NullPool)
    configure_engine(engine)
    configure_session(engine)

    session = get_session(engine)
    try:
        account = Account(username="op@example.com", is_default=True)
        session.add(account)
        await session.flush()
        session.add_all(
            Widget(id=subject_id)
            for subject_id in (7, 4242, 636363, 424242, 515151, 878787, 909090, 313131, 616161)
        )
        session.add(
            VaultSecret(
                kind=SecretKind.SUBSCRIPTION,
                audience=Audience.provider("anthropic"),
                account_id=account.id,
                identity={"email": account.username},
                secrets={"claudeAiOauth": {"accessToken": "t"}},
            )
        )
        await session.commit()
    finally:
        await session.close()

    @task
    async def record_task(repo: str) -> None:
        SINK.append(f"task:{repo}")

    @task(retries=1)
    async def retry_task() -> None:
        global TASK_RETRY_ATTEMPTS
        TASK_RETRY_ATTEMPTS += 1
        if TASK_RETRY_ATTEMPTS == 1:
            raise RuntimeError("retry task")
        SINK.append("task:retried")

    @task(every="0 6 * * *")
    async def scheduled_task() -> None:
        SINK.append("task:scheduled")

    class Approve(Gate):
        name = "approve"
        action: str = ""

        @classmethod
        async def on_wait(cls, workflow: Workflow) -> None:
            SINK.append("approve:notified")

    class Confirm(Gate):
        name = "confirm"
        action: str = ""

    class SampleFlow(Workflow):
        subject = Widget

        @step
        async def note_repo(self, repo: str) -> str:
            return f"recorded:{repo}"

        async def run_multistep(self, repo: str) -> None:
            await self.note_repo(repo)
            decision = await Approve.wait()
            if decision.action == "close":
                raise FatalError("closed at review")

    class AgentFlow(Workflow):
        DECIDER = Agent(id="decider", contract=Decision, prompt="t")

        async def run(self, repo: str) -> None:
            decision = await self.DECIDER(body="x")

            SINK.append(f"instep-journal:{len(self.journal.filter(Decision))}")
            if decision.action == "stop":
                raise FatalError("stopped by agent")

    class AgentBodyFlow(Workflow):
        async def run_multistep(self, repo: str) -> None:
            decision = await AgentFlow.DECIDER(body="x")
            assert self.journal.latest(Decision) is decision
            SINK.append(f"body-journal:{len(self.journal.filter(Decision))}:{decision.action}")

    class RecordFeedback(Workflow):
        @step
        async def note_repo(self, repo: str) -> None:
            SINK.append(repo)

        async def run_multistep(self, repo: str) -> None:
            await self.note_repo(repo)

    class RetryingStepFlow(Workflow):
        @step(retries=1)
        async def unreliable(self) -> None:
            global STEP_RETRY_ATTEMPTS
            STEP_RETRY_ATTEMPTS += 1
            if STEP_RETRY_ATTEMPTS == 1:
                raise RuntimeError("retry step")
            SINK.append("step:retried")

        async def run_multistep(self) -> None:
            await self.unreliable()

    class ChildFlow(Workflow):
        async def run(self) -> None:
            SINK.append("child:ran")

    class ParentFlow(Workflow):
        async def run_multistep(self) -> None:
            await ChildFlow.start(subject=None)

    class EnqueueInStepFlow(Workflow):
        @step
        async def misuse(self) -> None:
            await record_task.enqueue(repo="from-a-step")

        async def run_multistep(self) -> None:
            await self.misuse()

    class DailySweep(Workflow):
        every = "0 6 * * *"

        async def run(self) -> None:
            SINK.append("swept")

    class ScheduledDispatch(Workflow):
        subject = Widget
        every = "0 */4 * * *"

        @classmethod
        async def dispatch(cls) -> str:
            return await cls.start(subject=await Widget.get_for_id("313131"))

        async def run(self) -> None: ...

    class PolicyFlow(Workflow):
        subject = Widget

        @classmethod
        async def dispatch(cls) -> str:
            return await cls.start(subject=await Widget.get_for_id("616161"))

        async def run(self) -> None: ...

    class DispatchingFlow(Workflow):
        async def run_multistep(self) -> None:
            SINK.append(f"dispatched:{await PolicyFlow.dispatch()}")

    class SubjectFlow(Workflow):
        subject = Widget

        async def run(self) -> Decision:
            SINK.append(f"subj-id:{(await self.subject).id}")
            return Decision(action="ok")

    class DoubleGateFlow(Workflow):
        subject = Widget

        async def run_multistep(self) -> None:
            first = await Approve.wait()
            SINK.append(f"round1:{first.action}")
            second = await Approve.wait()
            SINK.append(f"round2:{second.action}")
            replies = [reply.action for reply in self.journal.filter(Approve)]
            SINK.append(f"gate-journal:{replies}")
            SINK.append("gate:completed")
            if SINK.count("gate:completed") == 1:
                raise asyncio.CancelledError

    class ConfirmFlow(Workflow):
        subject = Widget

        async def run_multistep(self) -> None:
            reply = await Confirm.wait()
            SINK.append(f"confirmed:{reply.action}")

    class SubjectlessConfirmFlow(Workflow):
        async def run_multistep(self) -> None:
            await Confirm.wait()

    class ReviewFlow(Workflow):
        async def run_multistep(self) -> None:
            await self.review()

    class AttributedFlow(Workflow):
        subject = Widget

        async def run_multistep(self) -> None:
            SINK.append(f"acct-before:{self.account_id}")
            await Approve.wait()
            SINK.append(f"acct-after:{self.account_id}")

    class AnnounceFlow(Workflow):
        subject = Widget

        async def run_multistep(self) -> None:
            await self.announce("test.revision", revision=1)
            await self.announce("test.revision", revision=2)
            marker = f"announced:{self.workflow_id}"
            SINK.append(marker)
            if SINK.count(marker) == 1:
                raise asyncio.CancelledError("Simulated worker interruption")

    os.environ["DRUKS_DATABASE_URL"] = URL
    init_dbos()
    await launch()
    try:
        yield SimpleNamespace(
            engine=engine,
            schema_engine=schema_engine,
            SampleFlow=SampleFlow,
            AgentFlow=AgentFlow,
            AgentBodyFlow=AgentBodyFlow,
            RecordFeedback=RecordFeedback,
            SubjectFlow=SubjectFlow,
            DoubleGateFlow=DoubleGateFlow,
            ConfirmFlow=ConfirmFlow,
            SubjectlessConfirmFlow=SubjectlessConfirmFlow,
            ReviewFlow=ReviewFlow,
            AttributedFlow=AttributedFlow,
            AnnounceFlow=AnnounceFlow,
            ScheduledDispatch=ScheduledDispatch,
            RetryingStepFlow=RetryingStepFlow,
            EnqueueInStepFlow=EnqueueInStepFlow,
            ParentFlow=ParentFlow,
            PolicyFlow=PolicyFlow,
            DispatchingFlow=DispatchingFlow,
            record_task=record_task,
            retry_task=retry_task,
            scheduled_task=scheduled_task,
        )
    finally:
        shutdown()
        await engine.dispose()
        schema_engine.dispose()

        agents._items.pop("decider", None)
        workflows._items.pop("sample_flow", None)
        workflows._items.pop("agent_flow", None)
        workflows._items.pop("agent_body_flow", None)
        workflows._items.pop("record_feedback", None)
        workflows._items.pop("daily_sweep", None)
        workflows._items.pop("subject_flow", None)
        workflows._items.pop("double_gate_flow", None)
        workflows._items.pop("confirm_flow", None)
        workflows._items.pop("subjectless_confirm_flow", None)
        workflows._items.pop("review_flow", None)
        workflows._items.pop("attributed_flow", None)
        workflows._items.pop("announce_flow", None)
        workflows._items.pop("scheduled_dispatch", None)
        workflows._items.pop("retrying_step_flow", None)
        workflows._items.pop("enqueue_in_step_flow", None)
        workflows._items.pop("child_flow", None)
        workflows._items.pop("parent_flow", None)
        if original_database_url is None:
            os.environ.pop("DRUKS_DATABASE_URL", None)
        else:
            os.environ["DRUKS_DATABASE_URL"] = original_database_url


async def _get_run(engine, workflow_id: str) -> Run | None:
    session = get_session(engine)
    try:
        return await session.get(Run, workflow_id)
    finally:
        await session.close()


async def _wait_for_run(engine, workflow_id, predicate, timeout=15.0):
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        row = await _get_run(engine, workflow_id)
        if row and predicate(row):
            return row
        await asyncio.sleep(0.1)
    raise AssertionError(f"timed out; last={await _get_run(engine, workflow_id)}")


async def _account_id(engine, email: str) -> str:
    session = get_session(engine)
    try:
        result = await session.execute(select(Account).where(Account.username == email))
        row = result.scalar_one_or_none()
        if not row:
            row = Account(username=email)
            session.add(row)
            await session.commit()
        return row.id
    finally:
        await session.close()


async def test_launch_commits_installation_settings_before_serving(runtime):
    async with get_session(runtime.engine) as session:
        settings = await session.get(InstallationSettings, 1)
        assert settings
        assert settings.default_harness == "claude"


async def test_attribution_rides_the_run_and_survives_resume(runtime):
    SINK.clear()
    account_id = await _account_id(runtime.engine, "op@example.com")
    workflow_id = await runtime.AttributedFlow.start(
        subject=Widget(id=878787), account_id=account_id
    )
    parked = await _wait_for_run(
        runtime.engine, workflow_id, lambda run: run.state == RunState.PARKED
    )
    with runtime.schema_engine.connect() as connection:
        attributes = connection.execute(
            select(workflow_status.c.attributes).where(
                workflow_status.c.workflow_uuid == workflow_id
            )
        ).scalar_one()
    assert attributes == {
        "subject_type": "widget",
        "subject_id": "878787",
        "subject_key": "W-878787",
        "subject_title": None,
    }
    assert parked.account_id == account_id
    assert f"acct-before:{account_id}" in SINK

    await parked.resume(action="go")
    await _wait_for_run(runtime.engine, workflow_id, lambda run: run.state == RunState.FINISHED)
    assert f"acct-after:{account_id}" in SINK


async def test_browser_origin_start_inherits_the_ambient_account(runtime):
    account_id = await _account_id(runtime.engine, "ambient@example.com")
    token = current_account_id.set(account_id)
    try:
        workflow_id = await runtime.RecordFeedback.start(subject=None, repo="owner/ambient")
    finally:
        current_account_id.reset(token)
    await _wait_for_run(runtime.engine, workflow_id, lambda run: run.state == RunState.FINISHED)
    assert (await _get_run(runtime.engine, workflow_id)).account_id == account_id


async def test_duplicate_start_shares_the_run_across_accounts(runtime):
    scheduled = []

    @subscribe(WorkflowEvent.SCHEDULED, workflow=runtime.SampleFlow)
    async def saw(*, subject: Widget, **_: object) -> None:
        scheduled.append(subject)

    first = await _account_id(runtime.engine, "op@example.com")
    second = await _account_id(runtime.engine, "peer@example.com")
    subject = Widget(id=909090)
    workflow_id = await runtime.SampleFlow.start(
        subject=subject, account_id=first, repo="owner/app"
    )
    parked = await _wait_for_run(
        runtime.engine, workflow_id, lambda run: run.state == RunState.PARKED
    )
    minted = [subject for subject in scheduled if subject.id == 909090]
    assert len(minted) == 1

    duplicate_id = await runtime.SampleFlow.start(
        subject=subject, account_id=second, repo="owner/app"
    )
    assert duplicate_id == workflow_id
    assert len([subject for subject in scheduled if subject.id == 909090]) == 1

    await parked.resume(action="merge")
    await _wait_for_run(runtime.engine, workflow_id, lambda run: run.state == RunState.FINISHED)


async def test_step_gate_resume_finish(runtime):
    workflow_id = await runtime.SampleFlow.start(subject=Widget(id=111111), repo="owner/app")

    parked = await _wait_for_run(
        runtime.engine, workflow_id, lambda run: run.state == RunState.PARKED
    )
    assert parked.input_gate == "approve"
    assert parked.input_requested_at is not None

    await parked.resume(action="merge")
    done = await _wait_for_run(
        runtime.engine, workflow_id, lambda run: run.state == RunState.FINISHED
    )
    assert done.input_gate is None
    assert done.failure is None


async def test_duplicate_replies_to_one_round_collapse(runtime):
    workflow_id = await runtime.DoubleGateFlow.start(subject=Widget(id=515151))
    parked = await _wait_for_run(
        runtime.engine, workflow_id, lambda run: run.state == RunState.PARKED
    )
    first_asked_at = parked.input_requested_at

    await parked.resume(action="first")
    await parked.resume(action="duplicate")

    with runtime.schema_engine.connect() as connection:
        delivered = connection.execute(
            text(
                "SELECT count(*) FROM dbos.notifications"
                " WHERE destination_uuid = :id AND topic = 'approve'"
            ),
            {"id": workflow_id},
        ).scalar_one()
    assert delivered == 1

    parked = await _wait_for_run(
        runtime.engine,
        workflow_id,
        lambda run: (
            run.state in (RunState.PARKED, RunState.FINISHED)
            and run.input_requested_at != first_asked_at
        ),
    )
    assert parked.state == RunState.PARKED
    assert "round1:first" in SINK

    second_asked_at = parked.input_requested_at
    await parked.resume(action="second")
    for _ in range(100):
        if "gate:completed" in SINK:
            break
        await asyncio.sleep(0.1)
    assert SINK.count("gate:completed") == 1
    await asyncio.sleep(0.2)
    await DBOS.resume_workflow_async(workflow_id)
    await _wait_for_run(runtime.engine, workflow_id, lambda run: run.state == RunState.FINISHED)
    assert SINK.count("gate:completed") == 2
    async with get_session(runtime.engine) as session:
        events = list(
            await session.scalars(
                select(Event).where(Event.payload["run"].astext == workflow_id).order_by(Event.id)
            )
        )
    requests = [event for event in events if event.type == "workflow.parked"]
    receipts = [
        event for event in events if event.type == "workflow.running" and "result" in event.payload
    ]
    rounds = [first_asked_at.isoformat(), second_asked_at.isoformat()]
    assert [event.payload["input_requested_at"] for event in requests] == rounds
    assert [event.payload["input_requested_at"] for event in receipts] == rounds
    assert [event.payload["result"] for event in receipts] == [
        {"action": "first"},
        {"action": "second"},
    ]
    assert "round2:second" in SINK
    assert "round2:duplicate" not in SINK

    assert "gate-journal:['first', 'second']" in SINK


async def test_fail_branch(runtime):
    workflow_id = await runtime.SampleFlow.start(subject=Widget(id=222222), repo="owner/app")
    parked = await _wait_for_run(
        runtime.engine, workflow_id, lambda run: run.input_gate == "approve"
    )

    await parked.resume(action="close")
    failed = await _wait_for_run(
        runtime.engine, workflow_id, lambda run: run.state == RunState.FAILED
    )
    assert failed.failure == "closed at review"

    with runtime.schema_engine.connect() as connection:
        status = connection.execute(
            text("SELECT status FROM dbos.workflow_status WHERE workflow_uuid = :id"),
            {"id": workflow_id},
        ).scalar_one()
    assert status == "ERROR"


async def test_signed_out_run_fails_and_marks_the_session_stale(runtime):
    session = get_session(runtime.engine)
    try:
        session.add(
            StoredBrowserSession(
                name="night_watch.acme",
                payload_format=BrowserSessionPayloadFormat.STORAGE_STATE.value,
                site="acme.example",
            )
        )
        await session.commit()
    finally:
        await session.close()

    class BounceFlow(Workflow):
        async def run(self) -> None:
            error = BrowserSessionSignedOutError("the site bounced the login")
            error.session_name = "night_watch.acme"
            raise error

    try:
        workflow_id = await BounceFlow.start(subject=None)
        failed = await _wait_for_run(
            runtime.engine, workflow_id, lambda run: run.state == RunState.FAILED
        )
        assert failed.failure == "the site bounced the login"
        assert failed.failure_code == "browser_session_signed_out"
        session = get_session(runtime.engine)
        try:
            stored = (
                await session.execute(
                    select(StoredBrowserSession).where(
                        StoredBrowserSession.name == "night_watch.acme"
                    )
                )
            ).scalar_one()
            assert stored.status == BrowserSessionStatus.STALE.value
        finally:
            await session.close()
    finally:
        workflows._items.pop("bounce_flow", None)


async def test_subjectless_gate_fails_loudly(runtime):
    workflow_id = await runtime.SubjectlessConfirmFlow.start(subject=None)
    failed = await _wait_for_run(
        runtime.engine, workflow_id, lambda run: run.state == RunState.FAILED
    )
    assert failed.failure
    assert "'confirm'" in failed.failure
    assert "subjectless" in failed.failure
    assert not failed.input_gate


async def test_subject_gate_parks_unchanged(runtime):
    workflow_id = await runtime.ConfirmFlow.start(subject=Widget(id=636363))
    parked = await _wait_for_run(
        runtime.engine, workflow_id, lambda run: run.state == RunState.PARKED
    )
    assert parked.input_gate == "confirm"

    with runtime.schema_engine.connect() as connection:
        attributes = connection.execute(
            select(workflow_status.c.attributes).where(
                workflow_status.c.workflow_uuid == workflow_id
            )
        ).scalar_one()
    assert attributes == {
        "subject_type": "widget",
        "subject_id": "636363",
        "subject_key": "W-636363",
        "subject_title": None,
    }

    await parked.resume(action="go")
    await _wait_for_run(runtime.engine, workflow_id, lambda run: run.state == RunState.FINISHED)
    assert "confirmed:go" in SINK


async def test_subjectless_review_fails_loudly(runtime):
    workflow_id = await runtime.ReviewFlow.start(subject=None)
    failed = await _wait_for_run(
        runtime.engine, workflow_id, lambda run: run.state == RunState.FAILED
    )
    assert failed.failure
    assert "'review'" in failed.failure


def _fake_ephemeral_returning(output: dict, seen: list[dict], held: list[bool]):
    @contextlib.asynccontextmanager
    async def _fake_ephemeral(self, **_kw):
        async def _run_agent(_session, **kwargs):
            seen.append(kwargs)

            held.append(db_session().in_transaction())

            return AgentResult(
                output=output,
                run_id=kwargs["call_id"],
                sandbox_host_id="host-test",
                model="claude",
                agent=kwargs["agent"],
                status=AgentCallStatus.SUCCEEDED,
                started_at=datetime.now(UTC),
            )

        yield SimpleNamespace(run_agent=_run_agent, id="host-test")

    return _fake_ephemeral


async def _fake_render(*_a, **_k):
    return "PROMPT"


async def test_run_agent_step(runtime, monkeypatch):
    seen: list[dict] = []
    held: list[bool] = []

    monkeypatch.setattr(
        "druks.sandbox.client.Client.ephemeral",
        _fake_ephemeral_returning({"action": "stop"}, seen, held),
    )
    monkeypatch.setattr("druks.agents.render_prompt", _fake_render)

    workflow_id = await runtime.AgentFlow.start(subject=None, repo="owner/app")
    failed = await _wait_for_run(
        runtime.engine, workflow_id, lambda run: run.state == RunState.FAILED
    )
    assert failed.failure == "stopped by agent"
    assert "instep-journal:0" in SINK
    assert seen[0]["artifact_dir"].name == f"run-{workflow_id}"
    assert seen[0]["agent"] == "decider"
    session = get_session(runtime.engine)
    try:
        recorded = list(
            (
                await session.execute(select(AgentCall).where(AgentCall.run_id == workflow_id))
            ).scalars()
        )
    finally:
        await session.close()

    assert recorded[0].id == seen[0]["call_id"]
    account_id = await _account_id(runtime.engine, "op@example.com")
    assert failed.account_id == account_id
    assert recorded[0].subscription.account_id == account_id
    assert recorded[0].api_key_id is None
    assert held == [False]


async def test_body_level_agent_output_lands_on_the_journal(runtime, monkeypatch):
    seen: list[dict] = []
    held: list[bool] = []
    monkeypatch.setattr(
        "druks.sandbox.client.Client.ephemeral",
        _fake_ephemeral_returning({"action": "ship"}, seen, held),
    )
    monkeypatch.setattr("druks.agents.render_prompt", _fake_render)

    workflow_id = await runtime.AgentBodyFlow.start(subject=None, repo="owner/app")
    await _wait_for_run(runtime.engine, workflow_id, lambda run: run.state == RunState.FINISHED)
    assert "body-journal:1:ship" in SINK


async def test_task_enqueue(runtime):
    SINK.clear()
    await runtime.RecordFeedback.start(subject=None, repo="owner/queued")
    deadline = asyncio.get_event_loop().time() + 15
    while "owner/queued" not in SINK and asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(0.1)
    assert "owner/queued" in SINK


async def test_durable_task_enqueue(runtime):
    SINK.clear()
    await runtime.record_task.enqueue(repo="owner/queued")
    deadline = asyncio.get_event_loop().time() + 15
    while "task:owner/queued" not in SINK and asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(0.1)
    assert "task:owner/queued" in SINK


async def test_durable_task_retries(runtime):
    global TASK_RETRY_ATTEMPTS
    TASK_RETRY_ATTEMPTS = 0
    SINK.clear()
    await runtime.retry_task.enqueue()
    deadline = asyncio.get_event_loop().time() + 15
    while "task:retried" not in SINK and asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(0.1)
    assert "task:retried" in SINK
    assert TASK_RETRY_ATTEMPTS > 1


async def test_step_retries(runtime):
    global STEP_RETRY_ATTEMPTS
    STEP_RETRY_ATTEMPTS = 0
    SINK.clear()
    workflow_id = await runtime.RetryingStepFlow.start(subject=None)
    await _wait_for_run(runtime.engine, workflow_id, lambda run: run.state == RunState.FINISHED)
    assert "step:retried" in SINK
    assert STEP_RETRY_ATTEMPTS > 1


async def test_db_session_on_a_task_with_no_bound_session_raises(runtime):
    async def read() -> None:
        db_session()

    with pytest.raises(SessionNotBoundError):
        await asyncio.create_task(read())


async def test_body_starts_a_child_run(runtime):
    workflow_id = await runtime.ParentFlow.start(subject=None)
    await _wait_for_run(runtime.engine, workflow_id, lambda row: row.state == RunState.FINISHED)

    deadline = asyncio.get_event_loop().time() + 15
    while asyncio.get_event_loop().time() < deadline:
        async with get_session(runtime.engine) as session:
            child = await session.scalar(select(Run).where(Run.kind == "child_flow"))
        if child and child.state == RunState.FINISHED:
            break
        await asyncio.sleep(0.1)
    assert child.state == RunState.FINISHED
    assert "child:ran" in SINK


async def test_body_dispatches_a_sibling_through_its_policy(runtime):
    workflow_id = await runtime.DispatchingFlow.start(subject=None)
    await _wait_for_run(runtime.engine, workflow_id, lambda row: row.state == RunState.FINISHED)
    dispatched = next(entry for entry in SINK if entry.startswith("dispatched:"))
    run = await _wait_for_run(
        runtime.engine,
        dispatched.removeprefix("dispatched:"),
        lambda row: row.state == RunState.FINISHED,
    )
    assert run.subject_key == "W-616161"


async def test_enqueue_inside_a_step_fails_the_run(runtime):
    workflow_id = await runtime.EnqueueInStepFlow.start(subject=None)
    failed = await _wait_for_run(
        runtime.engine, workflow_id, lambda run: run.state == RunState.FAILED
    )
    assert "inside a @step" in failed.failure


async def test_scheduled_task_runs_nullary_body(runtime):
    SINK.clear()
    await runtime.scheduled_task._scheduled_entry(datetime.now(UTC), None)
    assert "task:scheduled" in SINK


async def test_scheduled_task_must_be_nullary(runtime):
    with pytest.raises(WorkflowError, match="nullary"):

        @task(every="0 6 * * *")
        async def needs_argument(target: str) -> None: ...


async def test_task_name_uses_declaring_app(runtime):
    register_workflow_package("plain_task_package", "")
    register_workflow_package("app_task_package", "alpha")

    async def bare_task() -> None: ...

    bare_task.__module__ = "plain_task_package.tasks"
    bare = task(bare_task)

    async def summarize() -> None: ...

    summarize.__module__ = "app_task_package.tasks"
    namespaced = task(summarize)

    assert bare.name == "bare_task"
    assert namespaced.name == "alpha.summarize"


async def test_every_registers_schedule(runtime):
    workflow, entry = next(row for row in _scheduled if row[0].kind == "daily_sweep")
    assert workflow.every == "0 6 * * *"
    parameters = list(inspect.signature(entry).parameters.values())
    assert parameters[0].annotation is datetime
    assert len(parameters) == 2 and parameters[1].name == "context"


async def test_scheduled_tick_fires_dispatch_not_run(runtime):
    _, entry = next(row for row in _scheduled if row[0].kind == "scheduled_dispatch")
    await entry(datetime.now(UTC), None)

    deadline = asyncio.get_event_loop().time() + 15
    while asyncio.get_event_loop().time() < deadline:
        async with get_session(runtime.engine) as session:
            run = await session.scalar(select(Run).where(Run.kind == "scheduled_dispatch"))
        if run and run.state == RunState.FINISHED:
            break
        await asyncio.sleep(0.1)
    assert run.state == RunState.FINISHED
    assert run.subject_key == "W-313131"


async def test_scheduled_dispatch_must_be_nullary(runtime):
    with pytest.raises(WorkflowError, match="nullary"):

        class NeedsArg(Workflow):
            every = "0 6 * * *"

            async def run(self) -> None: ...  # pragma: no cover

            @classmethod
            async def dispatch(cls, target: str) -> str:  # pragma: no cover
                return target


async def test_apply_schedules_drops_undeclared(runtime):
    workflow, entry = next(row for row in _scheduled if row[0].kind == "daily_sweep")
    DBOS.create_schedule(schedule_name="stale_cron", workflow_fn=entry, schedule=workflow.every)
    assert "stale_cron" in {s["schedule_name"] for s in DBOS.list_schedules()}

    async with session_scope(runtime.engine) as session:
        await apply_schedules(session)

    live = {s["schedule_name"] for s in DBOS.list_schedules()}
    assert "stale_cron" not in live
    assert "daily_sweep" in live


async def test_apply_schedules_resolves_operator_overrides(runtime):
    async with session_scope(runtime.engine) as session:
        await SettingsOverride.write(session, "workflow:daily_sweep:schedule", "0 9 * * *")
    async with session_scope(runtime.engine) as session:
        await apply_schedules(session)
    schedule = await DBOS.get_schedule_async("daily_sweep")
    assert schedule["schedule"] == "0 9 * * *"

    async with session_scope(runtime.engine) as session:
        await SettingsOverride.write(session, "workflow:daily_sweep:schedule_enabled", False)
    async with session_scope(runtime.engine) as session:
        await apply_schedules(session)
    paused = await DBOS.get_schedule_async("daily_sweep")
    assert paused["status"] == "PAUSED"
    assert paused["schedule_id"] == schedule["schedule_id"]

    async with session_scope(runtime.engine) as session:
        await SettingsOverride.write(session, "workflow:daily_sweep:schedule", "0 10 * * *")
    async with session_scope(runtime.engine) as session:
        await apply_schedules(session)
    retuned = await DBOS.get_schedule_async("daily_sweep")
    assert retuned["status"] == "PAUSED"
    assert retuned["schedule"] == "0 10 * * *"
    assert retuned["schedule_id"] == schedule["schedule_id"]

    invocation = await trigger_schedule("daily_sweep")
    handle = await DBOS.retrieve_workflow_async(invocation)
    await asyncio.wait_for(handle.get_result(), timeout=15)
    assert "swept" in SINK
    async with session_scope(runtime.engine) as session:
        history = await session.execute(latest_invocations(["daily_sweep"], limit=8))
    assert [row.run for row in history] == [invocation]
    assert (await DBOS.get_schedule_async("daily_sweep"))["status"] == "PAUSED"

    async with session_scope(runtime.engine) as session:
        await SettingsOverride.write(session, "workflow:daily_sweep:schedule", None)
        await SettingsOverride.write(session, "workflow:daily_sweep:schedule_enabled", None)
    async with session_scope(runtime.engine) as session:
        await apply_schedules(session)
    restored = await DBOS.get_schedule_async("daily_sweep")
    assert restored["schedule"] == "0 6 * * *"
    assert restored["status"] == "ACTIVE"


async def test_session_scope_commits_writes(runtime):
    async with session_scope(runtime.engine) as session:
        await SettingsOverride.write(session, "session_scope_commit_probe", {"landed": True})

    session = get_session(runtime.engine)
    try:
        row = await session.get(SettingsOverride, "session_scope_commit_probe")
        assert row
        assert row.value == {"landed": True}
    finally:
        await session.close()


async def test_apply_schedules_evaluates_cron_in_installation_timezone(runtime, monkeypatch):
    async with session_scope(runtime.engine) as session:
        await apply_schedules(session)
    assert (await DBOS.get_schedule_async("daily_sweep"))["cron_timezone"] == "UTC"

    settings = engine.load_settings().model_copy(update={"timezone": "Europe/Madrid"})
    monkeypatch.setattr(engine, "load_settings", lambda: settings)
    async with session_scope(runtime.engine) as session:
        await apply_schedules(session)
    assert (await DBOS.get_schedule_async("daily_sweep"))["cron_timezone"] == "Europe/Madrid"


async def test_user_settings_get_recreates_the_singleton(runtime):
    async with session_scope(runtime.engine):
        await db_session().execute(delete(InstallationSettings))
    async with session_scope(runtime.engine) as session:
        assert (await InstallationSettings.get_or_create(session)).default_harness == "claude"
    async with session_scope(runtime.engine) as session:
        assert (await InstallationSettings.get_or_create(session)).id == 1


async def test_a_run_hydrates_the_subject_row_it_was_started_for(runtime):
    async with session_scope(runtime.engine):
        widget = Widget()
        db_session().add(widget)
        await db_session().flush()
        assert widget.identity == {"type": "widget", "id": widget.id}

        run = runtime.SubjectFlow()
        run._subject = widget.identity

        assert await run.subject is widget


async def test_input_is_validated_at_start(runtime):
    with pytest.raises(ValidationError):
        await runtime.SampleFlow.start(subject=Widget(id=333333), repo=1)
    with pytest.raises(WorkflowError):
        await runtime.SubjectFlow.start(subject=Widget(id=333333), repo="x")

    workflow_id = await runtime.RecordFeedback.start(subject=None, repo="owner/flat")
    await _wait_for_run(runtime.engine, workflow_id, lambda run: run.state == RunState.FINISHED)
    assert "owner/flat" in SINK


async def test_start_holds_a_run_to_the_declared_subject(runtime):
    with pytest.raises(WorkflowError, match="is about Widget, not Gadget"):
        await runtime.SubjectFlow.start(subject=Gadget(id="g1"))
    with pytest.raises(WorkflowError, match="is about Widget, not nothing"):
        await runtime.SubjectFlow.start(subject=None)
    with pytest.raises(WorkflowError, match="declares no subject"):
        await runtime.ReviewFlow.start(subject=Widget(id=999999))


async def test_run_signature_is_enforced(runtime):
    with pytest.raises(WorkflowError):

        class UntypedFlow(Workflow):
            async def run(self, repo) -> None: ...

    with pytest.raises(WorkflowError):

        class SplatFlow(Workflow):
            async def run(self, **kwargs: str) -> None: ...

    with pytest.raises(WorkflowError):

        class UntypedMultistepFlow(Workflow):
            async def run_multistep(self, repo) -> None: ...


async def test_a_workflow_declares_exactly_one_body(runtime):
    with pytest.raises(WorkflowError, match="exactly one is allowed"):

        class BothFlow(Workflow):
            async def run(self) -> None: ...
            async def run_multistep(self) -> None: ...

    with pytest.raises(WorkflowError, match="must define run"):

        class NeitherFlow(Workflow):
            async def other(self) -> None: ...


async def test_step_on_run_or_run_multistep_is_rejected(runtime):
    with pytest.raises(WorkflowError, match="doesn't take @step"):

        class StepOnRunFlow(Workflow):
            @step
            async def run(self) -> None: ...

    with pytest.raises(WorkflowError, match="must not be @step"):

        class StepOnMultistepFlow(Workflow):
            @step
            async def run_multistep(self) -> None: ...


async def test_subject_reaches_body_and_result_rides_finished_event(runtime):
    workflow_id = await runtime.SubjectFlow.start(subject=Widget(id=7))
    await _wait_for_run(runtime.engine, workflow_id, lambda run: run.state == RunState.FINISHED)

    assert "subj-id:7" in SINK

    session = get_session(runtime.engine)
    try:
        finished = (
            await session.execute(
                select(Event).where(Event.type == "workflow.finished", Event.subject_id == "7")
            )
        ).scalar_one()
    finally:
        await session.close()

    assert finished.payload["result"] == {"action": "ok"}


async def test_registry_rejects_duplicate_key(runtime):
    registry = Registry("test", key=lambda entry: entry["kind"])
    capability = {"kind": "k"}
    registry.register(capability)
    registry.register(capability)
    with pytest.raises(ValueError, match="durable identity"):
        registry.register({"kind": "k"})


async def test_run_events_carry_subject(runtime):
    workflow_id = await runtime.SubjectFlow.start(subject=Widget(id=4242))
    await _wait_for_run(runtime.engine, workflow_id, lambda run: run.state == RunState.FINISHED)

    session = get_session(runtime.engine)
    try:
        events = list(
            (
                await session.execute(
                    select(Event).where(Event.subject_id == "4242").order_by(Event.id)
                )
            ).scalars()
        )
    finally:
        await session.close()

    assert sorted(e.type for e in events) == [
        "workflow.finished",
        "workflow.running",
        "workflow.scheduled",
    ]
    assert {e.subject_type for e in events} == {"widget"}

    assert {e.subject_key for e in events} == {"W-4242"}
    assert all(e.payload["run"] == workflow_id for e in events)


async def test_duplicate_start_returns_the_live_run(runtime):
    subject = Widget(id=515151)
    workflow_id = await runtime.SampleFlow.start(subject=subject, repo="owner/app")
    parked = await _wait_for_run(
        runtime.engine, workflow_id, lambda run: run.state == RunState.PARKED
    )

    assert await runtime.SampleFlow.start(subject=subject, repo="owner/app") == workflow_id

    await parked.resume(action="merge")
    await _wait_for_run(runtime.engine, workflow_id, lambda run: run.state == RunState.FINISHED)

    deadline = asyncio.get_event_loop().time() + 10.0
    fresh = workflow_id
    while fresh == workflow_id and asyncio.get_event_loop().time() < deadline:
        fresh = await runtime.SampleFlow.start(subject=subject, repo="owner/app")
        if fresh == workflow_id:
            await asyncio.sleep(0.1)
    assert fresh != workflow_id


async def test_failed_enqueue_claims_no_slot(runtime, monkeypatch):
    subject = Widget(id=424242)

    async def enqueue_unavailable(*args, **kwargs):
        raise RuntimeError("queue down")

    with monkeypatch.context() as patched:
        patched.setattr("druks.workflows.run_queue.enqueue_async", enqueue_unavailable)
        with pytest.raises(RuntimeError, match="queue down"):
            await runtime.SubjectFlow.start(subject=subject)

    workflow_id = await runtime.SubjectFlow.start(subject=subject)
    await _wait_for_run(runtime.engine, workflow_id, lambda run: run.state == RunState.FINISHED)


async def test_subjectless_run_emits_no_events(runtime):
    workflow_id = await runtime.RecordFeedback.start(subject=None, repo="owner/quiet")
    await _wait_for_run(runtime.engine, workflow_id, lambda run: run.state == RunState.FINISHED)

    session = get_session(runtime.engine)
    try:
        rows = (await session.execute(select(Event))).scalars()
        events = [e for e in rows if e.payload.get("run") == workflow_id]
    finally:
        await session.close()

    assert events == []


async def test_announcements_survive_subscriber_retry_and_workflow_replay(runtime):
    deliveries = []

    @subscribe("test.revision", workflow=runtime.AnnounceFlow)
    async def receive(*, subject: Widget, revision: int) -> None:
        async with get_session(runtime.engine) as session:
            events = list(await session.scalars(select(Event).filter_by(type="test.revision")))
        deliveries.append((revision, len(events)))
        if len(deliveries) == 1:
            raise RuntimeError("Subscriber unavailable")

    workflow_id = await runtime.AnnounceFlow.start(subject=Widget(id=7))
    marker = f"announced:{workflow_id}"
    try:
        await _wait_for_run(runtime.engine, workflow_id, lambda run: SINK.count(marker) == 1)
        assert deliveries == [(1, 1), (1, 1), (2, 2)]

        await DBOS.resume_workflow_async(workflow_id)
        await _wait_for_run(runtime.engine, workflow_id, lambda run: run.state == RunState.FINISHED)
        assert SINK.count(marker) == 2

        async with get_session(runtime.engine) as session:
            events = list(
                await session.scalars(
                    select(Event).filter_by(type="test.revision").order_by(Event.id)
                )
            )
        assert [event.payload for event in events] == [
            {"revision": 1, "run": workflow_id, "kind": runtime.AnnounceFlow.kind},
            {"revision": 2, "run": workflow_id, "kind": runtime.AnnounceFlow.kind},
        ]
        assert deliveries == [(1, 1), (1, 1), (2, 2)]
    finally:
        await DBOS.cancel_workflow_async(workflow_id)


async def test_admission_commits_before_the_request_and_deduplicates(runtime):
    class AdmissionFlow(Workflow):
        subject = Widget

        async def run_multistep(self) -> None:
            await DBOS.recv_async("finish")

    subject = Widget(id=750750)
    workflow_id = ""
    try:
        with pytest.raises(ValueError, match="Roll back the request"):
            async with session_scope(runtime.engine):
                request_session = db_session()
                await request_session.execute(select(Widget).where(Widget.id == 750750))
                workflow_id = await AdmissionFlow.start(subject=subject)
                assert await AdmissionFlow.start(subject=subject) == workflow_id
                assert db_session() is request_session
                assert request_session.in_transaction()

                async with get_session(runtime.engine) as reader:
                    events = list(
                        await reader.scalars(
                            select(Event).filter_by(type="workflow.scheduled", subject_id="750750")
                        )
                    )
                assert len(events) == 1
                assert events[0].payload == {"run": workflow_id, "kind": AdmissionFlow.kind}
                assert events[0].subject_key == "W-750750"
                raise ValueError("Roll back the request")

        async with get_session(runtime.engine) as reader:
            events = list(
                await reader.scalars(
                    select(Event).filter_by(type="workflow.scheduled", subject_id="750750")
                )
            )
        assert len(events) == 1
    finally:
        if workflow_id:
            await DBOS.send_async(workflow_id, "done", topic="finish")
            await _wait_for_run(
                runtime.engine, workflow_id, lambda run: run.state == RunState.FINISHED
            )
        workflows._items.pop(AdmissionFlow.kind)


async def test_retry_reruns_the_step_whose_result_the_body_refused(runtime):
    calls = []

    class RefusedDelivery(Workflow):
        subject = Widget

        @step
        async def plan(self) -> None:
            calls.append("plan")

        @step
        async def deliver(self) -> str:
            calls.append("deliver")
            return "refused"

        async def run_multistep(self) -> None:
            await self.plan()
            if await self.deliver() == "refused":
                raise FatalError("delivery refused")

    try:
        first_id = await RefusedDelivery.start(subject=Widget(id=8))
        await _wait_for_run(runtime.engine, first_id, lambda run: run.state == RunState.FAILED)
        async with session_scope(runtime.engine) as session:
            first_run = await session.get(Run, first_id)
            retry_id = await first_run.retry()
        await _wait_for_run(runtime.engine, retry_id, lambda run: run.state == RunState.FAILED)

        assert calls == ["plan", "deliver", "deliver"]
        async with get_session(runtime.engine) as reader:
            retried = await reader.get(Run, retry_id)
        assert retried.failure == "delivery refused"
    finally:
        workflows._items.pop(RefusedDelivery.kind)


async def test_failed_retry_attempts_keep_separate_terminal_records(runtime):
    class FailingAttempt(Workflow):
        subject = Widget

        async def run(self) -> None:
            raise FatalError("Source unavailable")

    try:
        first_id = await FailingAttempt.start(subject=Widget(id=7))
        await _wait_for_run(runtime.engine, first_id, lambda run: run.state == RunState.FAILED)
        async with session_scope(runtime.engine) as session:
            first_run = await session.get(Run, first_id)
            retry_id = await first_run.retry()
        await _wait_for_run(runtime.engine, retry_id, lambda run: run.state == RunState.FAILED)

        assert retry_id != first_id
        async with get_session(runtime.engine) as reader:
            retried = await reader.get(Run, retry_id)
            events = list(await reader.scalars(select(Event).filter_by(type="workflow.failed")))
        assert retried.retry_from == first_id
        failures = [event for event in events if event.payload["run"] in {first_id, retry_id}]
        assert len(failures) == 2
        assert {event.payload["run"] for event in failures} == {first_id, retry_id}
        assert {event.payload["failure"] for event in failures} == {"Source unavailable"}
    finally:
        workflows._items.pop(FailingAttempt.kind)


async def test_output_event_survives_completed_step_replay(runtime, monkeypatch, tmp_path):
    monkeypatch.setenv("DRUKS_DATA_DIR", str(tmp_path))
    calls = []
    held = []
    monkeypatch.setattr(
        "druks.sandbox.client.Client.ephemeral",
        _fake_ephemeral_returning({"action": "reviewed"}, calls, held),
    )
    monkeypatch.setattr("druks.agents.render_prompt", _fake_render)
    completed = []

    class OutputFlow(Workflow):
        subject = Widget

        async def run_multistep(self) -> None:
            for _ in range(2):
                await runtime.AgentFlow.DECIDER(contract=ReviewResult, body="x")
            completed.append(self.workflow_id)
            if len(completed) == 1:
                raise asyncio.CancelledError

    workflow_id = await OutputFlow.start(subject=Widget(id=7))
    try:
        await _wait_for_run(runtime.engine, workflow_id, lambda run: len(completed) == 1)
        await DBOS.resume_workflow_async(workflow_id)
        await _wait_for_run(runtime.engine, workflow_id, lambda run: run.state == RunState.FINISHED)
        assert len(completed) == 2
        assert len(calls) == 2
        async with get_session(runtime.engine) as session:
            events = list(await session.scalars(select(Event).filter_by(type="review.completed")))
            artifacts = list(await session.scalars(select(Artifact)))
        assert len(events) == len(artifacts) == 2
        assert {event.payload["artifact_id"] for event in events} == {
            artifact.id for artifact in artifacts
        }
        assert {event.payload["agent_call_id"] for event in events} == {
            call["call_id"] for call in calls
        }
        assert {event.payload["run"] for event in events} == {workflow_id}
    finally:
        workflows._items.pop(OutputFlow.kind)


async def test_field_notes_activity_through_admission_review_failure_and_replay(
    runtime, monkeypatch, tmp_path
):
    monkeypatch.setenv("DRUKS_DATA_DIR", str(tmp_path))
    calls = []
    monkeypatch.setattr(
        "druks.sandbox.client.Client.ephemeral",
        _fake_ephemeral_returning({"gist": "The pump ran hot."}, calls, []),
    )
    monkeypatch.setattr("druks.agents.get_template_id", AsyncMock(return_value="template-test"))
    monkeypatch.setattr("druks.agents.render_prompt", _fake_render)
    completed = []
    body = Summarize.run_multistep

    async def interrupt_after_approval(self):
        await body(self)
        completed.append(self.workflow_id)
        if len(completed) == 1:
            raise asyncio.CancelledError

    monkeypatch.setattr(Summarize, "run_multistep", interrupt_after_approval)
    async with session_scope(runtime.engine):
        approved_note = await Note.create(body="The pump ran hot.")
        failed_note = await Note.create(body="The reading is unclear.")

    approved_id = await Summarize.dispatch(note=approved_note)
    await _wait_for_run(runtime.engine, approved_id, lambda run: run.is_parked)
    async with session_scope(runtime.engine):
        await OperatorReply.answer(approved_note, action="request_changes", note="Name the pump.")
    await _wait_for_run(runtime.engine, approved_id, lambda run: len(calls) == 2 and run.is_parked)
    async with session_scope(runtime.engine):
        await OperatorReply.answer(approved_note, action="approve")
    await _wait_for_run(runtime.engine, approved_id, lambda run: len(completed) == 1)
    await DBOS.resume_workflow_async(approved_id)
    await _wait_for_run(runtime.engine, approved_id, lambda run: run.state == RunState.FINISHED)

    monkeypatch.setattr(
        "druks.sandbox.client.Client.ephemeral", _fake_ephemeral_returning({}, calls, [])
    )
    failed_id = await Summarize.dispatch(note=failed_note)
    await _wait_for_run(runtime.engine, failed_id, lambda run: run.state == RunState.FAILED)

    assert completed == [approved_id, approved_id]
    async with session_scope(runtime.engine):
        assert (await Note.get_for_id(approved_note.id)).gist == "The pump ran hot."
        activity = list(
            await db_session().scalars(Event.get_history(app="field_notes").order_by(Event.id))
        )
    kinds = {
        run_id: [event.type for event in activity if event.payload.get("run") == run_id]
        for run_id in (approved_id, failed_id)
    }
    review_round = ["gist.prepared", "workflow.parked", "workflow.running"]
    assert kinds[approved_id] == [
        "workflow.scheduled",
        *review_round,
        *review_round,
        "note.gist_approved",
    ]
    assert kinds[failed_id] == ["workflow.scheduled", "workflow.failed"]

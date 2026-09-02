import asyncio
import contextlib
import os
from types import SimpleNamespace

import psycopg
import pytest
from druks.agents import Agent, AgentOutput
from druks.apps.registry import agents, workflows
from druks.database import configure_session, get_session
from druks.durable import FatalError, Run, RunState
from druks.durable.dbos_state import workflow_status
from druks.durable.engine import configure_engine, init_dbos, launch, shutdown
from druks.models import StoredSubject
from druks.testing import init_db
from druks.workflows import Gate, Subject, Workflow, step, task
from pydantic import BaseModel
from sqlalchemy import NullPool, create_engine, select
from sqlalchemy.ext.asyncio import create_async_engine

PG_BASE = os.environ.get("DRUKS_TEST_PG", "postgresql://druks:druks@localhost:5432")
DB = "druks_durable_test"
URL = f"{PG_BASE.replace('postgresql://', 'postgresql+psycopg://')}/{DB}"


def _pg_up() -> bool:
    try:
        psycopg.connect(f"{PG_BASE}/postgres", connect_timeout=2).close()
        return True
    except psycopg.Error:
        return False


pytestmark = [
    pytest.mark.skipif(not _pg_up(), reason="test Postgres not reachable"),
    pytest.mark.asyncio(loop_scope="module"),
]


class Decision(AgentOutput):
    # Module-level so DBOS can pickle it as a step result (authors' agent
    # contracts are module-level for the same reason). Not a registered
    # capability, so it doesn't pollute any registry.
    action: str


class RepoCfg(BaseModel):
    # The one typed run() input the test flows share.
    repo: str


SINK: list[str] = []
TASK_RETRY_ATTEMPTS = 0
STEP_RETRY_ATTEMPTS = 0


class Widget(StoredSubject):
    __tablename__ = "test_widgets"

    def get_label(self) -> str:
        return f"W-{self.id}"


class Gadget(Subject):
    # A second subject class, so "not the one this workflow declares" is a real case.
    pass


# run_multistep() below for fixtures using @step/a gate; run() for the rest.
def _build_units():
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
        # The on_wait override is what lets the subjectless flows below park
        # here at all — without it every wait() would fail as SubjectlessGate.
        name = "approve"
        action: str = ""

        @classmethod
        async def on_wait(cls, workflow: Workflow) -> None:
            SINK.append("approve:notified")

    class Confirm(Gate):
        # No on_wait override: only a subject run may park here.
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
            # run()'s whole body is one step, so this agent call is in-step and
            # must never land on the journal — on the live or the replay pass.
            SINK.append(f"instep-journal:{len(self.journal.filter(Decision))}")
            if decision.action == "stop":
                raise FatalError("stopped by agent")

    class AgentBodyFlow(Workflow):
        # In run_multistep the agent call is body-level — its own step — so
        # the platform journals the domain value it returns.
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

    class EnqueueInStepFlow(Workflow):
        # A retried step would enqueue again, so enqueue() must refuse in-step.
        @step
        async def misuse(self) -> None:
            await record_task.enqueue(repo="from-a-step")

        async def run_multistep(self) -> None:
            await self.misuse()

    # every= so launch()'s apply_schedules has a schedule to create (smoke).
    class DailySweep(Workflow):
        every = "0 6 * * *"

        async def run(self) -> None:  # pragma: no cover - not fired in tests
            SINK.append("swept")

    class ScheduledDispatch(Workflow):
        # every + a dispatch() classmethod: the tick fires dispatch(), never the
        # subjectless run(). dispatch() start()s for real — the enqueue must work
        # from the scheduled workflow's body (a step is forbidden the child-start).
        subject = Widget
        every = "0 */4 * * *"

        @classmethod
        async def dispatch(cls) -> str:
            return await cls.start(subject=await Widget.get_for_subject_id("313131"))

        async def run(self) -> None: ...

    class SubjectFlow(Workflow):
        # Records the subject the platform threaded in, and returns a BaseModel
        # so the result rides its workflow.finished event.
        subject = Widget

        async def run(self) -> Decision:
            SINK.append(f"subj-id:{(await self.subject).id}")
            return Decision(action="ok")

    class DoubleGateFlow(Workflow):
        # Two rounds on the same gate — the shape a stale buffered reply would
        # ghost-resume.
        async def run_multistep(self) -> None:
            first = await Approve.wait()
            SINK.append(f"round1:{first.action}")
            second = await Approve.wait()
            SINK.append(f"round2:{second.action}")
            replies = [reply.action for reply in self.journal.filter(Approve)]
            SINK.append(f"gate-journal:{replies}")

    class ConfirmFlow(Workflow):
        subject = Widget

        async def run_multistep(self) -> None:
            reply = await Confirm.wait()
            SINK.append(f"confirmed:{reply.action}")

    class SubjectlessConfirmFlow(Workflow):
        # The same no-on_wait gate, waited on by a run about nothing — nobody
        # would ever see the park, so it must fail instead.
        async def run_multistep(self) -> None:
            await Confirm.wait()

    class ReviewFlow(Workflow):
        async def run_multistep(self) -> None:
            await self.review()

    class AttributedFlow(Workflow):
        # Records the attributed account before and after a park — resume must
        # never swap the payer.
        subject = Widget

        async def run_multistep(self) -> None:
            SINK.append(f"acct-before:{self.account_id}")
            await Approve.wait()
            SINK.append(f"acct-after:{self.account_id}")

    return (
        SampleFlow,
        AgentFlow,
        AgentBodyFlow,
        RecordFeedback,
        SubjectFlow,
        DoubleGateFlow,
        ConfirmFlow,
        SubjectlessConfirmFlow,
        ReviewFlow,
        AttributedFlow,
        ScheduledDispatch,
        RetryingStepFlow,
        EnqueueInStepFlow,
        record_task,
        retry_task,
        scheduled_task,
    )


@pytest.fixture(scope="module", autouse=True)
async def rt():
    db_url_snap = os.environ.get("DRUKS_DATABASE_URL")

    admin = psycopg.connect(f"{PG_BASE}/postgres", autocommit=True)
    admin.execute(f"DROP DATABASE IF EXISTS {DB}")
    admin.execute(f"CREATE DATABASE {DB}")
    admin.close()

    schema_engine = create_engine(URL)
    init_db(schema_engine)  # full schema incl. durable_runs + the work_items chain
    engine = create_async_engine(URL, poolclass=NullPool)
    configure_engine(engine)
    configure_session(engine)

    # An agent run checks the resolved provider is connected before any VM work;
    # AgentFlow's decider resolves to claude, so connect anthropic for the module —
    # and mark the account as the execution fallback, the way the first
    # login would.
    from druks.accounts.models import Account
    from druks.harnesses.models import ProviderLogin
    from druks.user_settings.models import UserSettings

    session = get_session(engine)
    try:
        account = Account(username="op@example.com")
        session.add(account)
        await session.flush()
        session.add_all(
            Widget(id=subject_id)
            for subject_id in (7, 4242, 636363, 424242, 515151, 878787, 909090, 313131)
        )
        session.add(
            ProviderLogin(
                provider="anthropic",
                account_id=account.id,
                provider_email=account.username,
                kind="oauth",
                payload={"claudeAiOauth": {"accessToken": "t"}},
            )
        )
        await session.merge(
            UserSettings(id=UserSettings.SINGLETON_ID, fallback_account_id=account.id)
        )
        await session.commit()
    finally:
        await session.close()

    (
        sample_flow,
        agent_flow,
        agent_body_flow,
        feedback_flow,
        subject_flow,
        double_gate_flow,
        confirm_flow,
        subjectless_confirm_flow,
        review_flow,
        attributed_flow,
        scheduled_dispatch,
        retrying_step_flow,
        enqueue_in_step_flow,
        record_task,
        retry_task,
        scheduled_task,
    ) = _build_units()
    os.environ["DRUKS_DATABASE_URL"] = URL
    init_dbos()
    await launch()  # also runs await apply_schedules() for daily_sweep
    try:
        yield SimpleNamespace(
            engine=engine,
            schema_engine=schema_engine,
            SampleFlow=sample_flow,
            AgentFlow=agent_flow,
            AgentBodyFlow=agent_body_flow,
            RecordFeedback=feedback_flow,
            SubjectFlow=subject_flow,
            DoubleGateFlow=double_gate_flow,
            ConfirmFlow=confirm_flow,
            SubjectlessConfirmFlow=subjectless_confirm_flow,
            ReviewFlow=review_flow,
            AttributedFlow=attributed_flow,
            ScheduledDispatch=scheduled_dispatch,
            RetryingStepFlow=retrying_step_flow,
            EnqueueInStepFlow=enqueue_in_step_flow,
            record_task=record_task,
            retry_task=retry_task,
            scheduled_task=scheduled_task,
        )
    finally:
        shutdown()
        await engine.dispose()
        schema_engine.dispose()
        # Drop only the test's own keys so other modules see clean registries
        # (a wholesale restore would clobber registrations made meanwhile).
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
        workflows._items.pop("scheduled_dispatch", None)
        workflows._items.pop("retrying_step_flow", None)
        workflows._items.pop("enqueue_in_step_flow", None)
        if db_url_snap is None:
            os.environ.pop("DRUKS_DATABASE_URL", None)
        else:
            os.environ["DRUKS_DATABASE_URL"] = db_url_snap


async def _state(engine, workflow_id: str) -> Run | None:
    session = get_session(engine)
    try:
        return await session.get(Run, workflow_id)
    finally:
        await session.close()


async def _wait_for(engine, workflow_id, predicate, timeout=15.0):
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        row = await _state(engine, workflow_id)
        if row is not None and predicate(row):
            return row
        await asyncio.sleep(0.1)
    raise AssertionError(f"timed out; last={await _state(engine, workflow_id)}")


async def _account_id(engine, email: str) -> str:
    from druks.accounts.models import Account

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


async def test_attribution_rides_the_run_and_survives_resume(rt):
    """start(account_id=…) lands on the durable_runs row and the reserved
    input key; attributes stay subject-only; a resume never swaps the payer."""
    from druks.durable.dbos_state import workflow_status

    SINK.clear()
    account_id = await _account_id(rt.engine, "op@example.com")
    wfid = await rt.AttributedFlow.start(subject=Widget(id=878787), account_id=account_id)
    parked = await _wait_for(rt.engine, wfid, lambda r: r.state == RunState.PARKED)
    with rt.schema_engine.connect() as conn:
        attributes = conn.execute(
            select(workflow_status.c.attributes).where(workflow_status.c.workflow_uuid == wfid)
        ).scalar_one()
    assert attributes == {
        "subject_type": "widget",
        "subject_id": "878787",
        "subject_label": "W-878787",
    }
    assert parked.account_id == account_id
    assert f"acct-before:{account_id}" in SINK

    await parked.resume(action="go")
    await _wait_for(rt.engine, wfid, lambda r: r.state == RunState.FINISHED)
    assert f"acct-after:{account_id}" in SINK  # the resumer never becomes the payer


async def test_browser_origin_start_inherits_the_ambient_account(rt):
    # The auth gate stamps the request's account into a contextvar;
    # start() reads it when no explicit account_id is passed.
    from druks.accounts.context import current_account_id

    account_id = await _account_id(rt.engine, "ambient@example.com")
    token = current_account_id.set(account_id)
    try:
        wfid = await rt.RecordFeedback.start(subject=None, repo="owner/ambient")
    finally:
        current_account_id.reset(token)
    await _wait_for(rt.engine, wfid, lambda r: r.state == RunState.FINISHED)
    assert (await _state(rt.engine, wfid)).account_id == account_id


async def test_duplicate_start_shares_the_run_across_accounts(rt):
    # Attribution is NEVER part of the dedup id: two accounts starting the same
    # subject share the one active run — and only the mint announces
    # workflow.scheduled; the deduped start stays silent.
    from druks.durable.enums import WorkflowEvent
    from druks.signals import subscribe

    scheduled = []

    @subscribe(WorkflowEvent.SCHEDULED)
    async def saw(*, subject=None, **_: object) -> None:
        scheduled.append(subject)

    first = await _account_id(rt.engine, "op@example.com")
    second = await _account_id(rt.engine, "peer@example.com")
    subject = Widget(id=909090)
    wfid = await rt.SampleFlow.start(subject=subject, account_id=first, repo="owner/app")
    parked = await _wait_for(rt.engine, wfid, lambda r: r.state == RunState.PARKED)
    minted = [s for s in scheduled if s and s.get("id") == 909090]
    assert len(minted) == 1

    dup = await rt.SampleFlow.start(subject=subject, account_id=second, repo="owner/app")
    assert dup == wfid
    assert len([s for s in scheduled if s and s.get("id") == 909090]) == 1

    await parked.resume(action="merge")
    await _wait_for(rt.engine, wfid, lambda r: r.state == RunState.FINISHED)


async def test_step_gate_resume_finish(rt):
    wfid = await rt.SampleFlow.start(subject=Widget(id=111111), repo="owner/app")

    parked = await _wait_for(rt.engine, wfid, lambda r: r.state == RunState.PARKED)
    assert parked.input_gate == "approve"
    assert parked.input_requested_at is not None

    await parked.resume(action="merge")
    done = await _wait_for(rt.engine, wfid, lambda r: r.state == RunState.FINISHED)
    assert done.input_gate is None
    assert done.failure is None


async def test_duplicate_replies_to_one_round_collapse(rt):
    """Two replies to the same parked round yield one resume — the duplicate must
    not buffer on the topic and ghost-resume the gate's next round unprompted."""
    from sqlalchemy import text

    wfid = await rt.DoubleGateFlow.start(subject=None)
    parked = await _wait_for(rt.engine, wfid, lambda r: r.state == RunState.PARKED)
    first_asked_at = parked.input_requested_at

    # The race: both resumers read the run while parked, then both send.
    await parked.resume(action="first")
    await parked.resume(action="duplicate")

    # The duplicate collapsed against the round's one notification.
    with rt.schema_engine.connect() as conn:
        delivered = conn.execute(
            text(
                "SELECT count(*) FROM dbos.notifications"
                " WHERE destination_uuid = :id AND topic = 'approve'"
            ),
            {"id": wfid},
        ).scalar_one()
    assert delivered == 1

    # Round 2 parks fresh and waits for an operator — a ghost resume would have
    # answered it with the stale duplicate and finished the run.
    parked = await _wait_for(
        rt.engine,
        wfid,
        lambda r: (
            r.state in (RunState.PARKED, RunState.FINISHED)
            and r.input_requested_at != first_asked_at
        ),
    )
    assert parked.state == RunState.PARKED
    assert "round1:first" in SINK

    # A fresh reply to the new round is a new key, so it still gets through.
    await parked.resume(action="second")
    await _wait_for(rt.engine, wfid, lambda r: r.state == RunState.FINISHED)
    assert "round2:second" in SINK
    assert "round2:duplicate" not in SINK
    # Both replies landed on the journal, in reply order.
    assert "gate-journal:['first', 'second']" in SINK


async def test_fail_branch(rt):
    from sqlalchemy import text

    wfid = await rt.SampleFlow.start(subject=Widget(id=222222), repo="owner/app")
    parked = await _wait_for(rt.engine, wfid, lambda r: r.input_gate == "approve")

    await parked.resume(action="close")
    failed = await _wait_for(rt.engine, wfid, lambda r: r.state == RunState.FAILED)
    assert failed.failure == "closed at review"
    # FAILED derives from DBOS's own record: the FatalError re-raised out of the
    # workflow, so DBOS wrote terminal ERROR, not SUCCESS.
    with rt.schema_engine.connect() as conn:
        status = conn.execute(
            text("SELECT status FROM dbos.workflow_status WHERE workflow_uuid = :id"),
            {"id": wfid},
        ).scalar_one()
    assert status == "ERROR"


async def test_signed_out_run_fails_and_marks_the_session_stale(rt):
    # The platform owns the whole bounce reaction: the run fails under the code
    # and the stamped session row goes stale — committed apart from the failing
    # body's rolled-back transaction.
    from druks.browser.enums import BrowserSessionPayloadFormat, BrowserSessionStatus
    from druks.browser.exceptions import BrowserSessionSignedOutError
    from druks.browser.models import StoredBrowserSession

    session = get_session(rt.engine)
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
        wfid = await BounceFlow.start(subject=None)
        failed = await _wait_for(rt.engine, wfid, lambda r: r.state == RunState.FAILED)
        assert failed.failure == "the site bounced the login"
        assert failed.failure_code == "browser_session_signed_out"
        session = get_session(rt.engine)
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


async def test_subjectless_gate_fails_loudly(rt):
    """A gate with no on_wait override fails a subjectless run now, instead of
    parking it unseen for the whole gate TTL."""
    wfid = await rt.SubjectlessConfirmFlow.start(subject=None)
    failed = await _wait_for(rt.engine, wfid, lambda r: r.state == RunState.FAILED)
    assert failed.failure
    assert "'confirm'" in failed.failure
    assert "subjectless" in failed.failure
    assert not failed.input_gate  # refused up front — the run never parked


async def test_subject_gate_parks_unchanged(rt):
    """The same no-on_wait gate still parks and resumes for a subject run — the
    subject's watchers are the ones who'd see it, feed-side."""
    wfid = await rt.ConfirmFlow.start(subject=Widget(id=636363))
    parked = await _wait_for(rt.engine, wfid, lambda r: r.state == RunState.PARKED)
    assert parked.input_gate == "confirm"
    # start() stamped the subject as workflow attributes — the keying every
    # runs-for-a-subject query reads; the id normalizes to a string.
    with rt.schema_engine.connect() as conn:
        attributes = conn.execute(
            select(workflow_status.c.attributes).where(workflow_status.c.workflow_uuid == wfid)
        ).scalar_one()
    assert attributes == {
        "subject_type": "widget",
        "subject_id": "636363",
        "subject_label": "W-636363",
    }

    await parked.resume(action="go")
    await _wait_for(rt.engine, wfid, lambda r: r.state == RunState.FINISHED)
    assert "confirmed:go" in SINK


async def test_subjectless_review_fails_loudly(rt):
    """review() is a human gate too: a subjectless run fails instead of parking
    an in-app ask nobody would ever see."""
    wfid = await rt.ReviewFlow.start(subject=None)
    failed = await _wait_for(rt.engine, wfid, lambda r: r.state == RunState.FAILED)
    assert failed.failure
    assert "'review'" in failed.failure


def _fake_ephemeral_returning(action: str, seen: list[dict], held: list[bool]):
    # Class-method stand-in for Client.ephemeral: a VM whose agent returns
    # ``action``.
    from datetime import UTC, datetime

    from druks.durable.enums import AgentCallStatus
    from druks.sandbox.datastructures import AgentResult

    @contextlib.asynccontextmanager
    async def _fake_ephemeral(self, **_kw):
        async def _run_agent(**kwargs):
            seen.append(kwargs)
            # The agent runs for minutes in production, so the step commits before
            # handing over: its own session holds no connection through the wait.
            # Ask that session — sessions are task-scoped and the agent is awaited
            # in the step's task, so this is it. The pool cannot answer: it is
            # shared, and another reader in this instant is not this step pinning.
            from druks.database import db_session

            held.append(db_session().in_transaction())
            # The harness names the on-disk dir (and the row) from the supplied
            # call_id, so the result echoes it back as run_id.
            return AgentResult(
                output={"action": action},
                run_id=kwargs["call_id"],
                sandbox_host_id="host-test",
                model="claude",
                agent=kwargs["agent"],
                status=AgentCallStatus.SUCCEEDED,
                started_at=datetime.now(UTC),
            )

        # The base Workspace wrapping this box reads host_id off ``id``.
        yield SimpleNamespace(run_agent=_run_agent, id="host-test")

    return _fake_ephemeral


async def _fake_render(*_a, **_k):
    return "PROMPT"


async def test_run_agent_step(rt, monkeypatch):
    # Stub the VM; assert the step records an AgentCall and the result round-trips.
    from druks.durable.models import AgentCall

    seen: list[dict] = []
    held: list[bool] = []
    # Patch the class method (not the singleton instance): an instance-attr
    # patch leaves a shadowing leftover that breaks later sandbox tests.
    monkeypatch.setattr(
        "druks.sandbox.client.Client.ephemeral", _fake_ephemeral_returning("stop", seen, held)
    )
    monkeypatch.setattr("druks.agents.render_prompt", _fake_render)

    wfid = await rt.AgentFlow.start(subject=None, repo="owner/app")
    failed = await _wait_for(rt.engine, wfid, lambda r: r.state == RunState.FAILED)
    assert failed.failure == "stopped by agent"
    assert "instep-journal:0" in SINK  # an agent call inside run()'s step: never recorded
    assert seen and seen[0]["artifact_dir"].name == f"run-{wfid}"
    assert seen[0]["agent"] == "decider"
    session = get_session(rt.engine)
    try:
        recorded = list(
            (await session.execute(select(AgentCall).where(AgentCall.run_id == wfid))).scalars()
        )
    finally:
        await session.close()
    # The call is recorded under the orchestrator-minted id threaded to run_agent.
    assert recorded[0].id == seen[0]["call_id"]
    # No account on the start: the fallback account (the module's op@ seed)
    # is charged.
    assert recorded[0].account_id == await _account_id(rt.engine, "op@example.com")
    assert held == [False]  # the step let its connection go before the agent ran


async def test_body_level_agent_output_lands_on_the_journal(rt, monkeypatch):
    """A run_multistep body-level agent call lands the domain value the body
    receives on the journal — AgentBodyFlow asserts identity in-body and sinks
    the projection."""
    seen: list[dict] = []
    held: list[bool] = []
    monkeypatch.setattr(
        "druks.sandbox.client.Client.ephemeral", _fake_ephemeral_returning("ship", seen, held)
    )
    monkeypatch.setattr("druks.agents.render_prompt", _fake_render)

    wfid = await rt.AgentBodyFlow.start(subject=None, repo="owner/app")
    await _wait_for(rt.engine, wfid, lambda r: r.state == RunState.FINISHED)
    assert "body-journal:1:ship" in SINK


async def test_task_enqueue(rt):
    SINK.clear()
    await rt.RecordFeedback.start(subject=None, repo="owner/queued")
    deadline = asyncio.get_event_loop().time() + 15
    while "owner/queued" not in SINK and asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(0.1)
    assert "owner/queued" in SINK


async def test_durable_task_enqueue(rt):
    SINK.clear()
    await rt.record_task.enqueue(repo="owner/queued")
    deadline = asyncio.get_event_loop().time() + 15
    while "task:owner/queued" not in SINK and asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(0.1)
    assert "task:owner/queued" in SINK


async def test_durable_task_retries(rt):
    global TASK_RETRY_ATTEMPTS
    TASK_RETRY_ATTEMPTS = 0
    SINK.clear()
    await rt.retry_task.enqueue()
    deadline = asyncio.get_event_loop().time() + 15
    while "task:retried" not in SINK and asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(0.1)
    assert "task:retried" in SINK
    assert TASK_RETRY_ATTEMPTS > 1


async def test_step_retries(rt):
    global STEP_RETRY_ATTEMPTS
    STEP_RETRY_ATTEMPTS = 0
    SINK.clear()
    workflow_id = await rt.RetryingStepFlow.start(subject=None)
    await _wait_for(rt.engine, workflow_id, lambda run: run.state == RunState.FINISHED)
    assert "step:retried" in SINK
    assert STEP_RETRY_ATTEMPTS > 1


async def test_enqueue_inside_a_step_fails_the_run(rt):
    wfid = await rt.EnqueueInStepFlow.start(subject=None)
    failed = await _wait_for(rt.engine, wfid, lambda r: r.state == RunState.FAILED)
    assert "inside a @step" in failed.failure


async def test_scheduled_task_runs_nullary_body(rt):
    from datetime import UTC, datetime

    SINK.clear()
    await rt.scheduled_task._scheduled_entry(datetime.now(UTC), None)
    assert "task:scheduled" in SINK


async def test_scheduled_task_must_be_nullary(rt):
    from druks.durable.exceptions import WorkflowError

    with pytest.raises(WorkflowError, match="nullary"):

        @task(every="0 6 * * *")
        async def needs_argument(target: str) -> None: ...


async def test_task_name_uses_declaring_app(rt):
    from druks.apps.loader import register_workflow_package

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


async def test_every_registers_schedule(rt):
    # A Workflow with every= registers (schedule_name, cron, fn) so launch()'s
    # apply_schedules creates the DBOS cron. The fn must satisfy DBOS's
    # ScheduledWorkflow signature — (scheduled_at: datetime, context) — exactly,
    # or production crons silently stop firing.
    import inspect
    from datetime import datetime

    from druks.durable.engine import _scheduled

    entry = next((row for row in _scheduled if row[0].kind == "daily_sweep"), None)
    assert entry is not None, "daily_sweep did not register a schedule"
    cls, fn = entry
    assert cls.every == "0 6 * * *"
    params = list(inspect.signature(fn).parameters.values())
    assert params[0].annotation is datetime
    assert len(params) == 2 and params[1].name == "context"


async def test_scheduled_tick_fires_dispatch_not_run(rt):
    # A workflow that declares dispatch() is subject-backed — its run() can't fire
    # subjectless. The tick must reach dispatch(), and dispatch()'s start() must
    # enqueue the real run from the scheduled workflow's body.
    from datetime import UTC, datetime

    from druks.durable.engine import _scheduled

    _, fn = next(row for row in _scheduled if row[0].kind == "scheduled_dispatch")
    await fn(datetime.now(UTC), None)

    async def dispatched_run():
        session = get_session(rt.engine)
        try:
            return (
                await session.execute(select(Run).where(Run.kind == "scheduled_dispatch"))
            ).scalar_one_or_none()
        finally:
            await session.close()

    deadline = asyncio.get_event_loop().time() + 15
    while asyncio.get_event_loop().time() < deadline:
        run = await dispatched_run()
        if run and run.state == RunState.FINISHED:
            break
        await asyncio.sleep(0.1)
    assert run.state == RunState.FINISHED
    assert run.subject_label == "W-313131"  # about its subject, not subjectless


async def test_scheduled_dispatch_must_be_nullary(rt):
    # The tick fires dispatch() with no arguments, so a required parameter is a
    # declaration error, caught when the class is defined.
    from druks.durable.exceptions import WorkflowError

    with pytest.raises(WorkflowError, match="nullary"):

        class NeedsArg(Workflow):
            every = "0 6 * * *"

            async def run(self) -> None: ...  # pragma: no cover

            @classmethod
            async def dispatch(cls, target: str) -> str:  # pragma: no cover
                return target


async def test_apply_schedules_drops_undeclared(rt):
    # A schedule the sys-db still holds but no Workflow declares (a renamed or
    # removed cron) must be reconciled away, or it keeps firing a dead name.
    from dbos import DBOS
    from druks.durable.engine import _scheduled, apply_schedules

    cls, fn = next(row for row in _scheduled if row[0].kind == "daily_sweep")
    DBOS.create_schedule(schedule_name="stale_cron", workflow_fn=fn, schedule=cls.every)
    assert "stale_cron" in {s["schedule_name"] for s in DBOS.list_schedules()}

    await apply_schedules()

    live = {s["schedule_name"] for s in DBOS.list_schedules()}
    assert "stale_cron" not in live  # undeclared → dropped
    assert "daily_sweep" in live  # declared → preserved


async def test_apply_schedules_resolves_operator_overrides(rt):
    # The declared every= is the default cadence; a settings override retunes it and
    # schedule_enabled=False pauses it (no sys-db schedule at all). Clearing the
    # overrides reconciles back to the declared cron.
    from dbos import DBOS
    from druks.durable.engine import apply_schedules
    from druks.user_settings.models import SettingsOverride

    def sweep_cron():
        rows = {s["schedule_name"]: s["schedule"] for s in DBOS.list_schedules()}
        return rows.get("daily_sweep")

    from druks.database import session_scope

    # Each write commits — a bare test-task session stays idle-in-transaction
    # and its row locks deadlock any later test touching the same rows.
    async with session_scope(rt.engine):
        await SettingsOverride.write("workflow:daily_sweep:schedule", "0 9 * * *")
    await apply_schedules()
    assert sweep_cron() == "0 9 * * *"  # override wins over the declared default

    async with session_scope(rt.engine):
        await SettingsOverride.write("workflow:daily_sweep:schedule_enabled", False)
    await apply_schedules()
    assert sweep_cron() is None  # paused → no schedule, nothing fires

    async with session_scope(rt.engine):
        await SettingsOverride.write("workflow:daily_sweep:schedule", None)
        await SettingsOverride.write("workflow:daily_sweep:schedule_enabled", None)
    await apply_schedules()
    assert sweep_cron() == "0 6 * * *"  # overrides cleared → declared default


async def test_session_scope_commits_writes(rt):
    # A bare Session close rolls back, so without an explicit commit every
    # write under session_scope silently evaporates.
    from druks.database import session_scope
    from druks.user_settings.models import SettingsOverride

    async with session_scope(rt.engine):
        await SettingsOverride.write("session_scope_commit_probe", {"landed": True})

    session = get_session(rt.engine)
    try:
        row = await session.get(SettingsOverride, "session_scope_commit_probe")
        assert row is not None
        assert row.value == {"landed": True}
    finally:
        await session.close()


async def test_launch_commits_the_user_settings_seed(rt):
    # launch()'s reconcile touches the settings singleton (apply_schedules
    # reads its timezone), and the row must land committed before the app
    # serves: two requests racing the first-touch insert wait on its key lock
    # synchronously on the event loop and deadlock the whole process.
    from druks.user_settings.models import UserSettings

    session = get_session(rt.engine)
    try:
        assert await session.get(UserSettings, UserSettings.SINGLETON_ID) is not None
    finally:
        await session.close()


async def test_apply_schedules_evaluates_cron_in_operator_timezone(rt):
    # The cron is stored verbatim and evaluated in the operator's timezone, so
    # "daily at midnight" is their midnight and stays honest across DST.
    from dbos import DBOS
    from druks.durable.engine import apply_schedules
    from druks.user_settings.models import UserSettings

    def sweep_timezone():
        rows = {s["schedule_name"]: s["cron_timezone"] for s in DBOS.list_schedules()}
        return rows.get("daily_sweep")

    await apply_schedules()
    assert sweep_timezone() == "UTC"  # the settings default

    # Commit the write — a bare test-task session stays idle-in-transaction and
    # its row lock deadlocks any later test that touches user_settings.
    from druks.database import session_scope

    async with session_scope(rt.engine):
        await (await UserSettings.get()).update_profile(timezone="Europe/Madrid")
    await apply_schedules()
    assert sweep_timezone() == "Europe/Madrid"


async def test_user_settings_get_recreates_the_singleton(rt):
    # get() is the first-touch creator; its ON CONFLICT insert lets two
    # processes booting one fresh database both call it safely.
    from druks.database import db_session, session_scope
    from druks.user_settings.models import UserSettings
    from sqlalchemy import delete

    async with session_scope(rt.engine):
        await db_session().execute(delete(UserSettings))
    async with session_scope(rt.engine):
        assert (await UserSettings.get()).timezone == "UTC"
    async with session_scope(rt.engine):
        assert (await UserSettings.get()).id == UserSettings.SINGLETON_ID


async def test_a_run_hydrates_the_subject_row_it_was_started_for(rt):
    from druks.database import db_session, session_scope

    async with session_scope(rt.engine):
        widget = Widget()
        db_session().add(widget)
        await db_session().flush()
        assert widget.identity == {"type": "widget", "id": widget.id}

        run = rt.SubjectFlow()
        run._subject = widget.identity

        assert await run.subject is widget


async def test_input_is_validated_at_start(rt):
    # run()'s annotation is the wire contract: a bad input fails at start(), a
    # good one reaches the body through DBOS's own checkpointed arguments.
    from druks.durable import WorkflowError
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        await rt.SampleFlow.start(subject=Widget(id=333333), repo=1)  # wrong type
    with pytest.raises(WorkflowError):
        await rt.SubjectFlow.start(subject=Widget(id=333333), repo="x")  # takes no input

    wfid = await rt.RecordFeedback.start(subject=None, repo="owner/flat")
    await _wait_for(rt.engine, wfid, lambda r: r.state == RunState.FINISHED)
    assert "owner/flat" in SINK


async def test_start_holds_a_run_to_the_declared_subject(rt):
    # Subscribers derive their subject class from ``workflow=``, so the declaration
    # has to be true of every run — a wrong one, a missing one, and an unexpected
    # one all fail before anything is enqueued.
    from druks.durable import WorkflowError

    with pytest.raises(WorkflowError, match="is about Widget, not Gadget"):
        await rt.SubjectFlow.start(subject=Gadget(id="g1"))
    with pytest.raises(WorkflowError, match="is about Widget, not nothing"):
        await rt.SubjectFlow.start(subject=None)
    with pytest.raises(WorkflowError, match="declares no subject"):
        await rt.ReviewFlow.start(subject=Widget(id=999999))


async def test_run_signature_is_enforced(rt):
    # A body's parameters are the input contract — every one needs a type, and
    # *args/**kwargs are rejected at class definition, not at first start.
    from druks.durable import WorkflowError

    with pytest.raises(WorkflowError):

        class UntypedFlow(Workflow):
            async def run(self, repo) -> None: ...

    with pytest.raises(WorkflowError):

        class SplatFlow(Workflow):
            async def run(self, **kwargs: str) -> None: ...

    with pytest.raises(WorkflowError):

        class UntypedMultistepFlow(Workflow):
            async def run_multistep(self, repo) -> None: ...


async def test_a_workflow_declares_exactly_one_body(rt):
    # run()/run_multistep() are mutually exclusive, not two optional hooks.
    from druks.durable import WorkflowError

    with pytest.raises(WorkflowError, match="exactly one is allowed"):

        class BothFlow(Workflow):
            async def run(self) -> None: ...
            async def run_multistep(self) -> None: ...

    with pytest.raises(WorkflowError, match="must define run"):

        class NeitherFlow(Workflow):
            async def other(self) -> None: ...


async def test_step_on_run_or_run_multistep_is_rejected(rt):
    # run() is already the step; run_multistep() must stay unstepped.
    from druks.durable import WorkflowError

    with pytest.raises(WorkflowError, match="doesn't take @step"):

        class StepOnRunFlow(Workflow):
            @step
            async def run(self) -> None: ...

    with pytest.raises(WorkflowError, match="must not be @step"):

        class StepOnMultistepFlow(Workflow):
            @step
            async def run_multistep(self) -> None: ...


async def test_subject_reaches_body_and_result_rides_finished_event(rt):
    from druks.events.models import Event

    wfid = await rt.SubjectFlow.start(subject=Widget(id=7))
    await _wait_for(rt.engine, wfid, lambda r: r.state == RunState.FINISHED)

    assert "subj-id:7" in SINK  # the platform threaded subject into self.subject

    session = get_session(rt.engine)
    try:
        finished = (
            await session.execute(
                select(Event).where(Event.type == "workflow.finished", Event.subject_id == "7")
            )
        ).scalar_one()
    finally:
        await session.close()
    # run()'s BaseModel return rides the finished event.
    assert finished.payload["result"] == {"action": "ok"}


async def test_registry_rejects_duplicate_key(rt):
    # Re-registering the same item is idempotent, but a different capability on an
    # existing key is a collision (raises) — two can't share a durable identity.
    from druks.apps.registry import Registry

    registry = Registry("test", key=lambda entry: entry["kind"])
    capability = {"kind": "k"}
    registry.register(capability)
    registry.register(capability)  # same item re-imported → idempotent
    with pytest.raises(ValueError, match="durable identity"):
        registry.register({"kind": "k"})  # different capability, same kind → collision


async def test_run_events_carry_subject(rt):
    # The dispatch subject rides the run row (never a body arg), and every
    # run-state transition emits a run-level event keyed to it.
    from druks.events.models import Event

    wfid = await rt.SubjectFlow.start(subject=Widget(id=4242))
    await _wait_for(rt.engine, wfid, lambda r: r.state == RunState.FINISHED)

    session = get_session(rt.engine)
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

    assert [e.type for e in events] == ["workflow.running", "workflow.finished"]
    assert {e.subject_type for e in events} == {"widget"}
    # The label was stamped into the run's attributes at start and snapshotted
    # onto each transition — the identity itself stays the bare key.
    assert {e.subject_label for e in events} == {"W-4242"}
    assert all(e.payload["run"] == wfid for e in events)


async def test_duplicate_start_returns_the_live_run(rt):
    # One active run per subject+kind: the dedup slot is claimed at enqueue and
    # held while the run is enqueued, running, or parked — a duplicate start()
    # hands back the live run's id. The slot frees at the terminal outcome, so
    # the subject can run again.
    subject = Widget(id=515151)
    wfid = await rt.SampleFlow.start(subject=subject, repo="owner/app")
    parked = await _wait_for(rt.engine, wfid, lambda r: r.state == RunState.PARKED)

    assert await rt.SampleFlow.start(subject=subject, repo="owner/app") == wfid

    await parked.resume(action="merge")
    await _wait_for(rt.engine, wfid, lambda r: r.state == RunState.FINISHED)
    # DBOS clears the slot when the workflow's outcome commits, a beat after the
    # workflow.finished event — poll until the fresh start wins.
    deadline = asyncio.get_event_loop().time() + 10.0
    fresh = wfid
    while fresh == wfid and asyncio.get_event_loop().time() < deadline:
        fresh = await rt.SampleFlow.start(subject=subject, repo="owner/app")
        if fresh == wfid:
            await asyncio.sleep(0.1)
    assert fresh != wfid


async def test_failed_enqueue_claims_no_slot(rt, monkeypatch):
    # A failure at enqueue claims nothing — the next start() proceeds instead of
    # being handed a phantom "live" run DBOS never received.
    subject = Widget(id=424242)

    async def _boom(*args, **kwargs):
        raise RuntimeError("queue down")

    with monkeypatch.context() as patched:
        patched.setattr("druks.workflows.run_queue.enqueue_async", _boom)
        with pytest.raises(RuntimeError, match="queue down"):
            await rt.SubjectFlow.start(subject=subject)

    wfid = await rt.SubjectFlow.start(subject=subject)
    await _wait_for(rt.engine, wfid, lambda r: r.state == RunState.FINISHED)


async def test_subjectless_run_emits_no_events(rt):
    # Framework crons run without a subject — plumbing, not activity — so they
    # must not write run-level events into the feed.
    from druks.events.models import Event

    wfid = await rt.RecordFeedback.start(subject=None, repo="owner/quiet")
    await _wait_for(rt.engine, wfid, lambda r: r.state == RunState.FINISHED)

    session = get_session(rt.engine)
    try:
        rows = (await session.execute(select(Event))).scalars()
        events = [e for e in rows if e.payload.get("run") == wfid]
    finally:
        await session.close()

    assert events == []

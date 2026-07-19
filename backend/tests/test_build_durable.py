import asyncio
import os
from types import SimpleNamespace

import psycopg
import pytest
from conftest import init_db
from druks.database import configure_session, get_session
from druks.durable import Run, RunState
from druks.durable.engine import configure_engine, init_dbos, launch, shutdown
from sqlalchemy import create_engine

PG_BASE = os.environ.get("DRUKS_TEST_PG", "postgresql://druks:druks@localhost:5432")
DB = "druks_build_durable_test"
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


@pytest.fixture(scope="module", autouse=True)
def rt():
    from druks.extensions.loader import load
    from druks.extensions.registry import agents, workflows
    from fastapi import FastAPI

    # Load every extension first so the snapshot is the complete production registry;
    # the durable workflow then overwrites build's same-named agents, and the
    # wholesale restore puts the full set back. (Coexistence-only — the
    # collision is gone once the cutover deletes the old modules.)
    load(FastAPI())
    agents_snap, workflows_snap = dict(agents._items), dict(workflows._items)
    db_url_snap = os.environ.get("DRUKS_DATABASE_URL")

    admin = psycopg.connect(f"{PG_BASE}/postgres", autocommit=True)
    admin.execute(f"DROP DATABASE IF EXISTS {DB}")
    admin.execute(f"CREATE DATABASE {DB}")
    admin.close()

    engine = create_engine(URL)
    init_db(engine)
    configure_engine(engine)
    configure_session(engine)

    from druks.build.workflows import BuildWorkflow  # registers on import

    os.environ["DRUKS_DATABASE_URL"] = URL
    init_dbos()
    launch()
    try:
        yield SimpleNamespace(engine=engine, flow=BuildWorkflow)
    finally:
        shutdown()
        engine.dispose()
        agents._items, workflows._items = agents_snap, workflows_snap
        if db_url_snap is None:
            os.environ.pop("DRUKS_DATABASE_URL", None)
        else:
            os.environ["DRUKS_DATABASE_URL"] = db_url_snap


def _seed_work_item(engine, *, repo: str) -> int:
    # The run.* subscribers dereference the subject row (a subscriber failure
    # now fails the lifecycle step), so the item must exist, not just its id.
    from uuid import uuid4

    from druks.build.models import Project, WorkItem
    from sqlalchemy.orm import Session

    with Session(engine) as session:
        slug = f"rt-{uuid4().hex[:8]}"
        project = Project(name=slug, slug=slug)
        session.add(project)
        session.flush()
        item = WorkItem(project_id=project.id, repo=repo, title="rt")
        session.add(item)
        session.commit()
        return item.id


async def _wait(engine, wfid, predicate, timeout=20.0):
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        session = get_session(engine)
        try:
            row = session.get(Run, wfid)
            if row is not None and predicate(row):
                return row
        finally:
            session.close()
        await asyncio.sleep(0.1)
    raise AssertionError("timed out")


def _stub(monkeypatch, rt):
    import druks.build.workflows as m
    from druks.build.contracts import CodeReviewOutput, EvaluationOutput, PlanData
    from druks.build.enums import EvaluationVerdict, ReviewDecision
    from druks.build.policy import Gates, RepoPolicy

    flow = rt.flow

    async def _noop(*a, **k):
        return None

    for name in (
        "_push_ticket_status",
        "_set_pr_draft",
        "_request_assignee_review",
        "_clear_draft",
    ):
        monkeypatch.setattr(flow, name, _noop)

    async def _policy_and_profile(self):
        policy = RepoPolicy(
            gates=Gates(plan_approval="human", implementation_approval="human"),
            on_approval="merge",
        )
        return {"policy": policy.model_dump(mode="json"), "profile": {}}

    async def _settings(self):
        return flow.Settings(
            auto_dispatch_on_plan_approval=False, max_implementation_revisions=5, review_code=True
        )

    monkeypatch.setattr(flow, "_load_policy_and_profile", _policy_and_profile)
    monkeypatch.setattr(flow, "_load_settings", _settings)

    # The agent execution is faked per agent BELOW the step wrapper (_run, not
    # __call__), so every call still memoizes through DBOS exactly like prod —
    # the recovery test's call counts prove replay skips them. Pass-through
    # stages (generate_plan, review_code) return their stub as the step result,
    # so those stubs are the real domain models; contract-consuming stages take
    # attribute lookalikes. Returns the invocation log (agent ids, in order).
    results = {
        "generate_plan": PlanData(plan_markdown="p"),
        "review_plan": SimpleNamespace(
            decision=ReviewDecision.APPROVE, body="", assignee_github_login=None
        ),
        "implement": SimpleNamespace(
            status="success", base_sha="a", head_sha="b", branch="agent/acme-1", pr_number=42
        ),
        "evaluate_implementation": EvaluationOutput(
            verdict=EvaluationVerdict.PASS, body="", findings=[], checks=[], acceptance_results=[]
        ),
        "review_code": CodeReviewOutput(summary="ok"),
    }
    invoked: list[str] = []

    async def _run(self, **kwargs):
        invoked.append(self.id)
        return results[self.id]

    from druks.agents import Agent

    monkeypatch.setattr(Agent, "_run", _run)

    fake_github = SimpleNamespace(
        get_pull_request=lambda *a, **k: _dict({"state": "open"}),
        squash_merge_pull_request=lambda *a, **k: _dict({"merged": True}),
        create_issue_comment=_noop,
    )
    monkeypatch.setattr(m, "get_github_client", lambda *a, **k: fake_github)
    return invoked


async def _dict(d):
    return d


async def test_happy_path_to_merge(rt, monkeypatch):
    _stub(monkeypatch, rt)

    item_id = _seed_work_item(rt.engine, repo="acme/widget")
    wfid = await rt.flow.start(
        repo="acme/widget",
        subject={"type": "work_item", "id": item_id},
    )

    parked = await _wait(
        rt.engine,
        wfid,
        lambda r: r.state == RunState.PENDING_INPUT and r.input_gate == "review",
    )
    await parked.resume(action="approve", answers={})

    parked = await _wait(
        rt.engine,
        wfid,
        lambda r: r.state == RunState.PENDING_INPUT and r.input_gate == "review_work",
    )
    await parked.resume(action="approve")

    done = await _wait(rt.engine, wfid, lambda r: r.state == RunState.FINISHED)
    assert done.failure is None

    # Shipped settles via GitHub's pr.closed webhook (test_webhooks_pull_request),
    # not the run — the run's job ends at the merge call. The run's durable
    # residue is the event log of its state transitions.
    from druks.events.models import Event

    session = get_session(rt.engine)
    try:
        events = (
            session.query(Event).filter(Event.subject_id == str(item_id)).order_by(Event.id).all()
        )
    finally:
        session.close()
    assert [e.type for e in events if e.type.startswith("run.")][-1] == "run.finished"


async def test_recovery_rebuilds_the_diary_without_rerunning_agents(rt, monkeypatch):
    """The diary (plans, reviews, revision counts) is durable by determinism: a
    crash mid-run means DBOS re-executes run() from the top on a fresh instance,
    with every agent call and gate reply memoized. Kill the runtime while the run
    is parked mid-_plan_phase, bring it back up, and the run must finish — with
    the pre-crash agents replayed from checkpoints, never re-invoked."""
    from druks.durable.engine import init_dbos, launch, shutdown

    invoked = _stub(monkeypatch, rt)

    item_id = _seed_work_item(rt.engine, repo="acme/widget")
    wfid = await rt.flow.start(
        repo="acme/widget",
        subject={"type": "work_item", "id": item_id},
    )
    await _wait(
        rt.engine,
        wfid,
        lambda r: r.state == RunState.PENDING_INPUT and r.input_gate == "review",
    )
    assert invoked == ["generate_plan", "review_plan"]

    # The crash: tear the runtime down while the workflow is parked on the gate
    # and bring it back up. launch() recovers the pending workflow, which
    # replays run() from the top on a fresh BuildWorkflow instance.
    shutdown()
    init_dbos()
    launch()
    # A real crash restarts the process with a fresh event loop; this in-process
    # relaunch keeps the loop whose default executor destroy() just shut down.
    # Re-point it at the new instance (what the first DBOS async call would do)
    # before recovery's dequeue lands work on the dead one.
    from dbos import DBOS

    await DBOS._configure_asyncio_thread_pool()

    parked = await _wait(
        rt.engine,
        wfid,
        lambda r: r.state == RunState.PENDING_INPUT and r.input_gate == "review",
    )
    await parked.resume(action="approve", answers={})
    parked = await _wait(
        rt.engine,
        wfid,
        lambda r: r.state == RunState.PENDING_INPUT and r.input_gate == "review_work",
    )
    await parked.resume(action="approve")
    done = await _wait(rt.engine, wfid, lambda r: r.state == RunState.FINISHED)
    assert done.failure is None

    # Replay recomposition, proven by invocation counts: the pre-crash agents
    # came back from checkpoints (no re-invocation), and the post-crash phase ran
    # once each on the rebuilt diary — implement's revision guard, evaluate's
    # grade, and the review_code toggle all read recomposed state.
    assert invoked == [
        "generate_plan",
        "review_plan",
        "implement",
        "evaluate_implementation",
        "review_code",
    ]

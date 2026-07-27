import asyncio
import os
from types import SimpleNamespace

import psycopg
import pytest
from druks.contrib.ship.models import Project, WorkItem
from druks.database import configure_session, get_session
from druks.durable import Run, RunState
from druks.durable.engine import configure_engine, init_dbos, launch, shutdown
from druks.testing import init_db
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

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
    # the durable workflow then overwrites Ship's same-named agents, and the
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

    from druks.contrib.ship.workflows import Build  # registers on import

    os.environ["DRUKS_DATABASE_URL"] = URL
    init_dbos()
    launch()
    try:
        yield SimpleNamespace(engine=engine, flow=Build)
    finally:
        shutdown()
        engine.dispose()
        agents._items, workflows._items = agents_snap, workflows_snap
        if db_url_snap is None:
            os.environ.pop("DRUKS_DATABASE_URL", None)
        else:
            os.environ["DRUKS_DATABASE_URL"] = db_url_snap


def _seed_work_item(engine, *, repo: str):
    # The run.* subscribers dereference the subject row (a subscriber failure
    # now fails the lifecycle step), so the item must exist, not just its id.
    from uuid import uuid4

    with Session(engine) as session:
        name = f"rt-{uuid4().hex[:8]}"
        project = Project(name=name)
        session.add(project)
        session.flush()
        item = WorkItem(project_id=project.id, repo=repo, title="rt", ticket_key=name)
        session.add(item)
        session.commit()
        session.refresh(item)
        session.expunge(item)
        return item


async def _wait(engine, workflow_id, predicate, timeout=20.0):
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        session = get_session(engine)
        try:
            row = session.get(Run, workflow_id)
            if row and predicate(row):
                return row
        finally:
            session.close()
        await asyncio.sleep(0.1)
    raise AssertionError("timed out")


def _stub(
    monkeypatch,
    rt,
    *,
    plan_approval="human",
    auto_dispatch=False,
    merge_when_ready_accepted=True,
):
    import druks.contrib.ship.workflows as m
    from druks.contrib.ship.contracts import (
        AcceptanceCriterionOutput,
        CodeReviewOutput,
        EvaluationOutput,
        ImplementationOutput,
        PlanData,
        ReviewOutput,
    )
    from druks.contrib.ship.enums import EvaluationVerdict, ReviewDecision
    from druks.contrib.ship.policy import Gates, RepoPolicy

    flow = rt.flow

    async def _noop(*args, **kwargs):
        return

    for name in (
        "set_pr_draft",
        "request_assignee_review",
        "_clear_draft",
    ):
        monkeypatch.setattr(flow, name, _noop)

    async def _policy_and_profile(self):
        policy = RepoPolicy(
            gates=Gates(plan_approval=plan_approval, implementation_approval="human"),
            on_approval="merge",
        )
        return {"policy": policy.model_dump(mode="json"), "profile": {}}

    async def _settings(self):
        return flow.Settings(
            auto_dispatch_on_plan_approval=auto_dispatch,
            max_implementation_revisions=5,
            review_code=True,
        )

    monkeypatch.setattr(flow, "_load_policy_and_profile", _policy_and_profile)
    monkeypatch.setattr(flow, "_load_settings", _settings)

    # The agent execution is faked per agent BELOW the step wrapper (_run, not
    # __call__), so every call still memoizes through DBOS exactly like prod. The
    # stubs are real domain models: they land on the journal, so its typed
    # projections read them exactly as in prod. Returns the invocation log
    # (agent ids, in order).
    results = {
        # Acceptance criteria are what a human approve confirms — an empty plan
        # is never confirmed, so this fixture needs one to reach the work gate.
        "generate_plan": PlanData(
            plan_markdown="p",
            acceptance_criteria=[
                AcceptanceCriterionOutput(
                    id="AC-1", description="It ships.", verification="Read it."
                )
            ],
        ),
        "review_plan": ReviewOutput(decision=ReviewDecision.APPROVE, body=""),
        "implement": ImplementationOutput.model_validate(
            {
                "type": "result",
                "status": "success",
                "base_sha": "a",
                "head_sha": "b",
                "commit_sha": "b",
                "branch": "agent/acme-1",
                "pr_number": 42,
                "files_changed": [],
                "acceptance_results": [],
                "checks": [],
                "known_risks": [],
                "summary": "",
                "workspace_path": "/repo",
                "workspace_retention": None,
            }
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

    github_calls = []

    async def _merge_when_ready(repo, pr_number):
        github_calls.append(("merge_when_ready", repo, pr_number))
        return merge_when_ready_accepted

    fake_github = SimpleNamespace(
        merge_when_ready=_merge_when_ready,
        create_issue_comment=_noop,
    )
    monkeypatch.setattr(m, "get_github_client", lambda *args, **kwargs: fake_github)
    return invoked, github_calls


async def _start_to_work_gate(rt, item):
    workflow_id = await rt.flow.start(repo=item.repo, subject=item)
    parked = await _wait(
        rt.engine,
        workflow_id,
        lambda run: run.state == RunState.PARKED and run.input_gate == "review",
    )
    await parked.resume(action="approve", answers={})
    parked = await _wait(
        rt.engine,
        workflow_id,
        lambda run: run.state == RunState.PARKED and run.input_gate == "review_work",
    )
    return workflow_id, parked


async def test_happy_path_declares_merge_intent(rt, monkeypatch):
    _, github_calls = _stub(monkeypatch, rt)

    item = _seed_work_item(rt.engine, repo="acme/widget")
    workflow_id, parked = await _start_to_work_gate(rt, item)
    await parked.resume(action="approve")

    done = await _wait(rt.engine, workflow_id, lambda run: run.state == RunState.FINISHED)
    assert not done.failure
    # The merge intent carries the journal-backed number of the first delivery.
    assert github_calls == [("merge_when_ready", "acme/widget", 42)]

    # The pr.opened announce mirrored the delivery onto the item.
    with Session(rt.engine) as session:
        refreshed = session.get(WorkItem, item.id)
        assert (refreshed.pr_number, refreshed.branch) == (42, "agent/acme-1")

    # Shipped settles via GitHub's pr.closed webhook (test_webhooks_pull_request),
    # not the run — the run's job ends when GitHub accepts the merge intent. The durable
    # residue is the event log of its state transitions.
    from druks.events.models import Event

    session = get_session(rt.engine)
    try:
        events = (
            session.query(Event).filter(Event.subject_id == str(item.id)).order_by(Event.id).all()
        )
    finally:
        session.close()
    assert [e.type for e in events if e.type.startswith("workflow.")][-1] == "workflow.finished"


async def test_rejected_merge_intent_reparks_work_gate(rt, monkeypatch):
    _, github_calls = _stub(
        monkeypatch,
        rt,
        merge_when_ready_accepted=False,
    )

    item = _seed_work_item(rt.engine, repo="acme/repark")
    workflow_id, parked = await _start_to_work_gate(rt, item)
    first_parked_at = parked.input_requested_at
    await parked.resume(action="approve")

    reparked = await _wait(
        rt.engine,
        workflow_id,
        lambda run: (
            run.state == RunState.PARKED
            and run.input_gate == "review_work"
            and run.input_requested_at != first_parked_at
        ),
    )
    assert not reparked.failure
    assert [call[0] for call in github_calls] == ["merge_when_ready"]
    await reparked.cancel()


async def test_auto_mode_machine_review_replaces_the_plan_gate(rt, monkeypatch):
    """plan_approval resolves to none: the machine reviewer approves the plan and
    the run reaches the work gate with no plan park — review_plan runs exactly
    once, where it substitutes for the operator."""
    invoked, _ = _stub(monkeypatch, rt, plan_approval=None, auto_dispatch=True)

    item = _seed_work_item(rt.engine, repo="acme/gizmo")
    workflow_id = await rt.flow.start(
        repo="acme/gizmo",
        subject=item,
    )

    parked = await _wait(
        rt.engine,
        workflow_id,
        lambda run: run.state == RunState.PARKED and run.input_gate == "review_work",
    )
    assert invoked[:2] == ["generate_plan", "review_plan"]
    await parked.resume(action="approve")
    done = await _wait(rt.engine, workflow_id, lambda run: run.state == RunState.FINISHED)
    assert not done.failure

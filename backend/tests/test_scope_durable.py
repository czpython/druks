import asyncio
import os
from types import SimpleNamespace

import psycopg
import pytest
from conftest import init_db
from druks.database import configure_session, db_session, get_session
from druks.durable import Run, RunState
from druks.durable.engine import configure_engine, init_dbos, launch, shutdown
from sqlalchemy import create_engine

PG_BASE = os.environ.get("DRUKS_TEST_PG", "postgresql://druks:druks@localhost:5432")
DB = "druks_scope_durable_test"
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

    from druks.build.workflows import Scope  # registers on import

    os.environ["DRUKS_DATABASE_URL"] = URL
    init_dbos()
    launch()
    try:
        yield SimpleNamespace(engine=engine, flow=Scope)
    finally:
        shutdown()
        engine.dispose()
        agents._items, workflows._items = agents_snap, workflows_snap
        if db_url_snap is None:
            os.environ.pop("DRUKS_DATABASE_URL", None)
        else:
            os.environ["DRUKS_DATABASE_URL"] = db_url_snap


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


def _brief(status, *, questions=()):
    from druks.build.contracts import ScopeBriefOutput

    return ScopeBriefOutput(
        status=status,
        problem="Workers can't see progress.",
        scope="Add SSE.",
        acceptance_criteria=["streams events"],
        stack_hints=["python"],
        related_repos=[],
        out_of_scope=[],
        decisions=[],
        open_questions=list(questions),
    )


async def test_scope_parks_then_resumes_to_ready(rt, monkeypatch):
    from druks.build.enums import HandoffStatus
    from druks.build.models import Project, ProjectRepo, WorkItem
    from druks.events.models import Event

    project = Project.create(name="acme/widget")
    ProjectRepo.create(project_id=project.id, full_name="acme/widget")
    item_id = WorkItem.create(
        project_id=project.id,
        source="linear",
        title="Add SSE",
        remote_key="ACME-1",
        repo="acme/widget",
    ).id
    db_session().commit()

    # Round 1 asks a question (parks); round 2 is ready (finishes). The agent
    # owns the tracker writes, so nothing else is stubbed.
    sequence = iter([_brief("needs_answers", questions=["Which transport?"]), _brief("ready")])

    async def _run_agent(self, **kwargs):
        return next(sequence)

    monkeypatch.setattr("druks.agents.Agent._run", _run_agent)

    wfid = await rt.flow.start(
        subject=WorkItem.subject_for(item_id),
        source="linear",
        remote_key="ACME-1",
    )

    parked = await _wait(
        rt.engine,
        wfid,
        lambda r: r.state == RunState.PENDING_INPUT and r.input_gate == "scope",
    )
    # The ask is declared at the gate, stored on the run — the read side renders it.
    assert parked.input_request == {
        "presentation": "external",
        "label": "Answer scope questions",
    }

    await parked.resume()  # the operator answered on the ticket; wake and re-scope

    done = await _wait(rt.engine, wfid, lambda r: r.state == RunState.FINISHED)
    assert done.failure is None

    session = get_session(rt.engine)
    try:
        finished = (
            session.query(Event)
            .filter(Event.type == "run.finished", Event.subject_id == str(item_id))
            .one()
        )
        item_status = session.get(WorkItem, item_id).status
    finally:
        session.close()
    # The finished event carries the result; the lane subscriber reacted to it.
    assert finished.payload["result"]["status"] == "ready"
    assert item_status == HandoffStatus.SCOPED

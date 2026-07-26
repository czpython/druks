import pytest
from conftest import seed_run
from druks.api.artifacts import get_artifact
from druks.durable.enums import RunState
from druks.durable.models import AgentCall, Artifact, Run
from druks.durable.schemas import RunResponse
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert


def _seed_call(db_session) -> AgentCall:
    db_session.add(Run(id="run-1", kind="build"))
    call = AgentCall(id="call-1", run_id="run-1", sandbox_host_id="host-1")
    db_session.add(call)
    db_session.flush()
    return call


def test_record_writes_content_and_descriptor(db_session, tmp_path):
    _seed_call(db_session)
    Artifact.record(
        call_dir=tmp_path,
        call_id="call-1",
        kind="markdown",
        title="Implementation plan",
        content="# Plan\nbody",
    )
    artifact = Artifact.get_for_call("call-1")
    assert artifact is not None
    assert (artifact.kind, artifact.title, artifact.path) == (
        "markdown",
        "Implementation plan",
        "artifact.md",
    )
    assert (tmp_path / "artifact.md").read_text() == "# Plan\nbody"


def test_record_is_idempotent_per_call(db_session, tmp_path):
    # A replayed step must not double-record; the unique fk makes record a no-op.
    _seed_call(db_session)
    for _ in range(2):
        Artifact.record(
            call_dir=tmp_path, call_id="call-1", kind="markdown", title="P", content="x"
        )
    rows = db_session.scalars(select(Artifact).where(Artifact.agent_call_id == "call-1")).all()
    assert len(rows) == 1


def test_artifact_cascades_with_its_call(db_session, tmp_path):
    call = _seed_call(db_session)
    Artifact.record(call_dir=tmp_path, call_id="call-1", kind="markdown", title="P", content="x")
    assert Artifact.get_for_call("call-1") is not None

    db_session.delete(call)
    db_session.flush()
    assert Artifact.get_for_call("call-1") is None


def test_get_latest_for_run_returns_the_newest_calls_artifact(db_session, tmp_path):
    # The read side serves the run's latest artifact on the in-app review ask —
    # the second call's plan wins.
    db_session.add(Run(id="run-1", kind="build"))
    for call_id, title in (("call-1", "First plan"), ("call-2", "Revised plan")):
        db_session.add(AgentCall(id=call_id, run_id="run-1", sandbox_host_id="host-1"))
        db_session.flush()
        Artifact.record(
            call_dir=tmp_path / call_id,
            call_id=call_id,
            kind="markdown",
            title=title,
            content="x",
        )
    latest = Artifact.get_latest_for_run("run-1")
    assert latest is not None and latest.title == "Revised plan"


def test_get_ask_resolves_the_review_artifact(db_session, tmp_path):
    # An in-app ask stores no label/artifact — the read side derives both from
    # the run's latest artifact. A declared ask passes through untouched.
    run = Run(
        id="run-1",
        kind="build",
        state=RunState.PARKED.value,
        input_request={"presentation": "in_app", "controls": ["approve"]},
    )
    db_session.add(run)
    db_session.add(AgentCall(id="call-1", run_id="run-1", sandbox_host_id="host-1"))
    db_session.flush()
    Artifact.record(call_dir=tmp_path, call_id="call-1", kind="markdown", title="Plan", content="x")

    ask = RunResponse.from_run(run, [], input_request=run.get_ask()).input_request
    assert ask == {
        "presentation": "in_app",
        "controls": ["approve"],
        "label": "Review: Plan",
        "artifact_id": Artifact.get_for_call("call-1").id,
    }

    external = Run(
        id="run-2",
        kind="build",
        state=RunState.PARKED.value,
        input_request={"presentation": "external", "label": "Review implementation"},
    )
    db_session.add(external)
    db_session.flush()
    response = RunResponse.from_run(external, [], input_request=external.get_ask())
    assert response.input_request == {
        "presentation": "external",
        "label": "Review implementation",
    }


def test_run_response_projects_the_parked_gate(db_session):
    run = seed_run(db_session, "run-gate", input_gate="review")

    response = RunResponse.from_run(run, [], input_request=None)
    assert response.gate == "review"


async def test_get_artifact_returns_recorded_content(db_session, tmp_path, monkeypatch):
    # call_dir resolves through load_settings().artifacts_dir, so point it at tmp.
    monkeypatch.setenv("DRUKS_DATA_DIR", str(tmp_path))
    db_session.add(Run(id="run-1", kind="build"))
    call = AgentCall(id="call-1", run_id="run-1", sandbox_host_id="host-1")
    db_session.add(call)
    db_session.flush()
    Artifact.record(
        call_dir=call.call_dir,
        call_id="call-1",
        kind="markdown",
        title="Implementation plan",
        content="# Plan\nbody",
    )
    result = await get_artifact(Artifact.get_for_call("call-1").id)
    assert (result.kind, result.title, result.content) == (
        "markdown",
        "Implementation plan",
        "# Plan\nbody",
    )


async def test_get_artifact_404_when_missing(db_session):
    with pytest.raises(HTTPException) as exc:
        await get_artifact("nope")
    assert exc.value.status_code == 404


async def test_get_artifact_404_when_content_gone(db_session, tmp_path, monkeypatch):
    # Descriptor row present but the file never written — the read repairs to a 404,
    # not a 500.
    monkeypatch.setenv("DRUKS_DATA_DIR", str(tmp_path))
    db_session.add(Run(id="run-1", kind="build"))
    db_session.add(AgentCall(id="call-1", run_id="run-1", sandbox_host_id="host-1"))
    db_session.flush()
    db_session.execute(
        pg_insert(Artifact).values(
            id="art-1", agent_call_id="call-1", kind="markdown", title="P", path="artifact.md"
        )
    )
    db_session.flush()
    with pytest.raises(HTTPException) as exc:
        await get_artifact("art-1")
    assert exc.value.status_code == 404

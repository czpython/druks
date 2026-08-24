from pathlib import Path

import pytest
from druks.accounts.context import current_account_id
from druks.apps.base import App
from druks.database import db_session
from druks.durable import AgentCall, Run
from druks.durable.datastructures import Subject
from druks.durable.schemas import SubjectSummary
from druks.models import StoredSubject
from druks.testing import asgi_client, seed_dbos_status
from fastapi import APIRouter
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import event, select
from uuid_utils import uuid7


class _ThingSummary(SubjectSummary):
    title: str


TITLES = {1: "First", 2: "Second"}


class Thing(StoredSubject):
    __tablename__ = "faketest_things"

    def get_summary(self) -> _ThingSummary:
        return _ThingSummary(id=self.id, label=self.label, title=TITLES[self.id])

    @classmethod
    async def list_summaries(cls, account_id: str | None) -> list[_ThingSummary]:
        return [thing.get_summary() for thing in await db_session().scalars(select(cls))]


class Ticket(Subject):
    """The other half of the contract: a subject with no row, whose id spans the
    separators a URL path is cut on."""

    @classmethod
    async def get_for_subject_id(cls, subject_id: str) -> "Ticket | None":
        if "#" in subject_id:
            return cls(id=subject_id)
        return

    def get_summary(self) -> _ThingSummary:
        return _ThingSummary(id=self.id, label=self.label, title=self.id.rpartition("#")[2])

    @classmethod
    async def list_summaries(cls, account_id: str | None) -> list[_ThingSummary]:
        return [ticket.get_summary() for ticket in await cls.list_open()]


CALLERS: list[str | None] = []


class Inbox(Subject):
    """A board scoped by who is asking."""

    @classmethod
    async def list_summaries(cls, account_id: str | None) -> list[_ThingSummary]:
        CALLERS.append(account_id)
        return []


class _ThingApp(App):
    name = "faketest"


async def _seed_run(
    session,
    *,
    subject_id,
    subject_type="thing",
    kind="faketest.flow",
    state="running",
    input_request=None,
    input_gate=None,
    failure=None,
):
    if state == "parked" and not input_gate:
        input_gate = "review"
    run = Run(
        id=str(uuid7()),
        kind=kind,
        input_gate=input_gate,
        input_request=input_request,
        failure=failure,
    )
    session.add(run)
    await session.flush()
    await seed_dbos_status(session, run.id, state, subject={"type": subject_type, "id": subject_id})
    return run


async def _seed_call(session, run, *, agent, status="succeeded"):
    call = AgentCall(run_id=run.id, agent=agent, model="m", status=status, sandbox_host_id="h")
    session.add(call)
    await session.flush()
    return call


@pytest.fixture
async def client(tmp_path: Path, druks_db, monkeypatch):
    # The real app mounts every app's routers before its catch-all 404, so the
    # fake app's router has to slot in there too — appending lands after the
    # catch-all and gets shadowed. Pulled back out on teardown; the app is a singleton.
    from druks.testing import configure_app_for_test, make_settings

    monkeypatch.setenv("DRUKS_DATA_DIR", str(tmp_path))
    for subject_id in TITLES:
        await druks_db.merge(Thing(id=subject_id))
    await druks_db.flush()
    app = configure_app_for_test(settings=make_settings(tmp_path))

    holder = APIRouter()
    for subject_class in (Thing, Ticket):
        holder.include_router(_ThingApp._get_subject_routes(subject_class), prefix="/api/faketest")
    catchall = next(
        i for i, r in enumerate(app.routes) if getattr(r, "path", "") == "/api/{path:path}"
    )
    for route in reversed(holder.routes):
        app.router.routes.insert(catchall, route)
    try:
        async with asgi_client(app) as test_client:
            yield test_client
    finally:
        for route in holder.routes:
            app.router.routes.remove(route)


def test_a_subject_id_is_a_string_whatever_the_row_is_keyed_by(druks_db):
    # A row is keyed by an integer and every read of a subject id is a string — the
    # URL segment, the DBOS attribute, the dedup key. The header takes either and is
    # always the string, so no summary hand-stringifies a primary key.
    assert SubjectSummary.model_validate(Thing(id=7)).id == "7"
    assert SubjectSummary.model_validate(Ticket(id="owner/repo#7")).id == "owner/repo#7"

    # Only the id widens: a title that arrives as a number is still a mistake.
    with pytest.raises(ValidationError):
        _ThingSummary(id=7, label="7", title=7)


def test_a_summary_carries_the_subjects_own_label(druks_db):
    assert SubjectSummary.model_validate(Thing(id=7)).label == "thing 7"
    assert SubjectSummary.model_validate(Ticket(id="owner/repo#7")).label == "owner/repo#7"

    # A title nobody can read is what this field exists to prevent, so a missing
    # one and a blank one fail the same way.
    with pytest.raises(ValidationError):
        _ThingSummary(id=1, title="First")
    with pytest.raises(ValidationError):
        _ThingSummary(id=1, label="   ", title="First")


async def test_status_aggregates_across_runs_and_timeline_spans_them(client: TestClient, druks_db):
    # Subject "1" lived across two runs: an earlier finished one and a current
    # running one. Status is the newest run's, and the timeline is every run,
    # oldest first, each carrying its own agent calls.
    done = await _seed_run(druks_db, subject_id="1", kind="faketest.prepare", state="finished")
    await _seed_call(druks_db, done, agent="prepare")
    live = await _seed_run(druks_db, subject_id="1", state="running")
    await _seed_call(druks_db, live, agent="implement", status="running")

    detail = (await client.get("/api/faketest/thing/1")).json()
    assert detail["summary"] == {"id": "1", "label": "thing 1", "title": "First"}
    assert detail["status"]["state"] == "running"
    assert [entry["kind"] for entry in detail["timeline"]] == ["faketest.prepare", "faketest.flow"]
    # Calls group under their own run, not the subject at large.
    assert [c["agent"] for c in detail["timeline"][0]["agentCalls"]] == ["prepare"]
    assert [c["agent"] for c in detail["timeline"][1]["agentCalls"]] == ["implement"]


async def test_parked_run_surfaces_needs_you(client: TestClient, druks_db):
    run = await _seed_run(
        druks_db,
        subject_id="1",
        state="parked",
        input_gate="approve_plan",
        input_request={"label": "Approve the plan"},
    )
    await _seed_call(druks_db, run, agent="generate_plan")

    detail = (await client.get("/api/faketest/thing/1")).json()
    assert detail["status"]["state"] == "parked"
    assert detail["status"]["gate"] == "approve_plan"
    parked = detail["timeline"][-1]
    assert parked["inputRequest"] == {"label": "Approve the plan"}


async def test_status_carries_the_latest_run_failure(client: TestClient, druks_db):
    # A failed subject exposes its stop reason on the status, so a board can render
    # "why" without walking the timeline. An active or finished subject carries none.
    await _seed_run(druks_db, subject_id="1", state="failed", failure="profiler boom")

    status = (await client.get("/api/faketest/thing/1")).json()["status"]
    assert status["state"] == "failed"
    assert status["failure"] == "profiler boom"

    await _seed_run(druks_db, subject_id="2", state="running")
    running = (await client.get("/api/faketest/thing/2")).json()["status"]
    assert running["failure"] is None


async def test_parked_board_row_skips_the_agent_call_query(client: TestClient, druks_db):
    # A parked row's status carries its gate ask, never its latest agent call, so
    # the per-subject status read must not load agent_calls — the board runs it
    # for every subject.
    run = await _seed_run(
        druks_db,
        subject_id="1",
        state="parked",
        input_gate="approve_plan",
        input_request={"label": "Approve the plan"},
    )
    await _seed_call(druks_db, run, agent="generate_plan")

    call_reads: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany):
        if "agent_calls" in statement and statement.lstrip().upper().startswith("SELECT"):
            call_reads.append(statement)

    engine = druks_db.bind.sync_connection
    event.listen(engine, "before_cursor_execute", record)
    try:
        body = (await client.get("/api/faketest/thing")).json()
    finally:
        event.remove(engine, "before_cursor_execute", record)

    rows = {row["summary"]["id"]: row for row in body["rows"]}
    assert rows["1"]["status"]["gate"] == "approve_plan"
    assert call_reads == []


async def test_list_returns_every_subject_with_status(client: TestClient, druks_db):
    live = await _seed_run(druks_db, subject_id="1", state="running")
    await _seed_call(druks_db, live, agent="implement", status="running")

    body = (await client.get("/api/faketest/thing")).json()
    rows = {row["summary"]["id"]: row for row in body["rows"]}
    assert rows["1"]["summary"]["title"] == "First"
    assert rows["1"]["status"]["state"] == "running"
    # "2" has no runs yet — it still lists, defaulting to scheduled.
    assert rows["2"]["status"]["state"] == "scheduled"


async def test_the_board_and_its_stream_hand_the_caller_to_list_summaries(druks_db):
    # The route reads the caller at handler entry and passes it down as data.
    # The stream keeps that caller after the request context ends.
    endpoints = {
        route.path: route.endpoint for route in _ThingApp._get_subject_routes(Inbox).routes
    }

    CALLERS.clear()
    token = current_account_id.set("acct-7")
    try:
        await endpoints["/inbox"]()
        response = await endpoints["/inbox/stream"](engine=druks_db.bind)
    finally:
        current_account_id.reset(token)
    assert CALLERS == ["acct-7"]

    await anext(response.body_iterator)
    await response.body_iterator.aclose()
    assert CALLERS == ["acct-7", "acct-7"]


async def test_unknown_subject_is_404(client: TestClient, druks_db):
    assert (await client.get("/api/faketest/thing/nope")).status_code == 404
    # An id the subject could never wear misses the same way, row or no row.
    assert (await client.get("/api/faketest/ticket/nope")).status_code == 404


async def test_an_id_spanning_separators_reaches_the_board_and_its_page(
    client: TestClient, druks_db
):
    # A row-less subject's id is free text — "owner/repo#7" carries the path
    # separator and the fragment marker, and both reads still key on the whole id.
    await _seed_run(druks_db, subject_type="ticket", subject_id="owner/repo#7", state="parked")

    board = (await client.get("/api/faketest/ticket")).json()
    assert [row["summary"]["id"] for row in board["rows"]] == ["owner/repo#7"]

    detail = (await client.get("/api/faketest/ticket/owner/repo%237")).json()
    assert detail["summary"] == {"id": "owner/repo#7", "label": "owner/repo#7", "title": "7"}
    assert detail["status"]["state"] == "parked"
    assert [entry["kind"] for entry in detail["timeline"]] == ["faketest.flow"]


@pytest.mark.parametrize("path", ["thing/nope", "ticket/owner/nope"])
async def test_a_subjects_stream_wins_over_the_greedy_id_matcher(
    client: TestClient, druks_db, path
):
    # The id matcher spans separators, so ``/stream`` has to stay a suffix and not
    # get swallowed into the id — whatever shape the id is. A stream for a subject
    # that names nothing closes at once, which is what proves it got there.
    response = await client.get(f"/api/faketest/{path}/stream")

    assert response.status_code == 200
    assert response.text == ""


@pytest.mark.parametrize("subject_class", [Thing, Ticket])
def test_the_literal_routes_are_declared_ahead_of_the_id_matcher(subject_class):
    # FastAPI matches in declaration order; both boards would be unreachable if the
    # id matcher came first.
    router = _ThingApp._get_subject_routes(subject_class)
    prefix = f"/{subject_class.subject_type}"

    assert [route.path for route in router.routes] == [
        prefix,
        f"{prefix}/stream",
        f"{prefix}/{{subject_id:path}}/stream",
        f"{prefix}/{{subject_id:path}}",
    ]

import pytest
from conftest import make_test_work_item, seed_dbos_status
from druks.build.models import Project, ProjectRepo, WorkItem
from druks.build.workflows import Scope, ScopeReply
from druks.durable import Run
from druks.ticketing.datastructures import Ticket
from druks.workflows import WorkflowError
from uuid_utils import uuid7


@pytest.fixture
def _stub_enqueue(monkeypatch):
    """Stub the durable start so the dispatch path runs without a live DBOS
    runtime. Records each call and hands back a fake run id."""
    calls: list[dict] = []

    async def _fake_start(**kwargs):
        calls.append(kwargs)
        return str(uuid7())

    monkeypatch.setattr("druks.build.workflows.Scope.start", _fake_start)
    return calls


def _scope_run(db_session, *, work_item_id, parked=True):
    run = Run(
        id=str(uuid7()),
        kind="build.scope",
        input_gate=ScopeReply.name if parked else None,
    )
    db_session.add(run)
    db_session.flush()
    seed_dbos_status(
        db_session,
        run.id,
        "pending_input" if parked else "finished",
        subject={"type": "work_item", "id": work_item_id},
    )
    return run


@pytest.mark.asyncio
async def test_answers_parked_scope_by_subject(db_session, monkeypatch):
    item = make_test_work_item(repo="acme/widget", title="t", source="linear", remote_key="ACME-1")
    run = _scope_run(db_session, work_item_id=item.id)
    answered = []

    async def answer(self, **reply):
        answered.append((self.id, reply))

    monkeypatch.setattr(Run, "resume", answer)

    await ScopeReply.answer(item)

    assert answered == [(run.id, {})]


@pytest.mark.asyncio
async def test_rejects_other_work_items(db_session):
    item = make_test_work_item(repo="acme/widget", title="t", source="linear", remote_key="ACME-1")
    _scope_run(db_session, work_item_id=item.id)
    with pytest.raises(WorkflowError, match="is not parked"):
        await ScopeReply.answer(WorkItem(id=item.id + 1))


@pytest.mark.asyncio
async def test_rejects_a_resolved_scope(db_session):
    item = make_test_work_item(repo="acme/widget", title="t", source="linear", remote_key="ACME-1")
    _scope_run(db_session, work_item_id=item.id, parked=False)
    with pytest.raises(WorkflowError, match="is not parked"):
        await ScopeReply.answer(item)


@pytest.mark.asyncio
async def test_transition_skips_an_already_labeled_ticket_without_a_run(db_session, _stub_enqueue):
    """The scoped label is the done-marker: dispatching anyway would enqueue a
    run for a ticket that's already scoped — noise."""
    project = Project.create(name="acme/widget")
    ProjectRepo.create(project_id=project.id, full_name="acme/widget")
    db_session.flush()

    ticket = Ticket(
        provider="linear",
        id="uuid-1",
        key="ACME-9",
        title="t",
        project_name="widget",
        labels=["druks-scoped"],
    )
    assert await Scope.dispatch(ticket=ticket) is None
    assert _stub_enqueue == []  # nothing enqueued


@pytest.mark.asyncio
async def test_transition_scopes_an_unlabeled_ticket(db_session, _stub_enqueue):
    project = Project.create(name="acme/widget")
    ProjectRepo.create(project_id=project.id, full_name="acme/widget")
    db_session.flush()

    ticket = Ticket(
        provider="linear",
        id="uuid-1",
        key="ACME-9",
        title="t",
        project_name="widget",
        labels=["bug"],
    )
    run_id = await Scope.dispatch(ticket=ticket)

    assert run_id is not None
    assert len(_stub_enqueue) == 1
    # Identity only crosses the wire — the agent fetches the rest itself.
    assert _stub_enqueue[0] == {
        "subject": _stub_enqueue[0]["subject"],
        "account_id": None,
        "remote_key": "ACME-9",
        "source": "linear",
    }


@pytest.mark.asyncio
async def test_label_routed_ticket_lands_on_the_work_item(db_session, _stub_enqueue):
    """The org-project shape: the Jira project names the org, a label names the
    repo. Routing resolves at dispatch and lands on the work item — the run's
    prompt context reads it from there."""
    project = Project.create(name="octo/alfred")
    ProjectRepo.create(project_id=project.id, full_name="octo/alfred")
    db_session.flush()

    ticket = Ticket(
        provider="jira",
        id="10001",
        key="SHRP-40586",
        title="t",
        project_name="Octo",
        labels=["Alfred"],
    )
    assert await Scope.dispatch(ticket=ticket) is not None
    item = WorkItem.get_for_remote_key(source="jira", remote_key="SHRP-40586")
    assert item.repo == "octo/alfred"


@pytest.mark.asyncio
async def test_unroutable_ticket_creates_nothing(db_session, _stub_enqueue):
    ticket = Ticket(
        provider="jira",
        id="10002",
        key="SHRP-9",
        title="t",
        project_name="Octo",
        labels=["bug"],
    )
    assert await Scope.dispatch(ticket=ticket) is None
    assert _stub_enqueue == []
    assert WorkItem.get_for_remote_key(source="jira", remote_key="SHRP-9") is None

import druks.contrib.software_factory.subscribers  # noqa: F401
import pytest
from conftest import connect_service
from druks.contrib.software_factory.app import SoftwareFactory
from druks.contrib.software_factory.contracts import ReviewWork
from druks.contrib.software_factory.issues.enums import Status
from druks.contrib.software_factory.issues.models import Ticket
from druks.contrib.software_factory.models import Project, ProjectRepo, WorkItem
from druks.contrib.software_factory.ticketing.enums import TicketStatus
from druks.contrib.software_factory.ticketing.issues import IssuesTracker
from druks.contrib.software_factory.workflows import Build
from druks.core.apis.exceptions import UnknownTicketError

from software_factory.factories import make_test_work_item, seed_build_run


async def _open_ticket(*, project="Acme", prefix="WID", full_name="acme/widget", title="one"):
    row = await Project.create(name=project, prefix=prefix)
    repo = await ProjectRepo.create(project_id=row.id, full_name=full_name)
    return await Ticket.create(repo_id=repo.id, title=title)


def _pin_software_factory_settings(monkeypatch, **values):
    settings = SoftwareFactory.Settings(**values)

    async def _settings(cls):
        return settings

    monkeypatch.setattr(SoftwareFactory, "settings", classmethod(_settings))


async def _connect_github() -> None:
    await connect_service(
        "github",
        identity={"app_id": "1", "slug": "druks-operator"},
        secrets={"private_key": "operator-pem", "webhook_secret": "hook-secret"},
    )


@pytest.mark.parametrize(
    ("asked", "board"),
    [
        (TicketStatus.TRIGGER, Status.READY_FOR_AGENT),
        (TicketStatus.IN_PROGRESS, Status.IN_PROGRESS),
        (TicketStatus.IN_REVIEW, Status.IN_REVIEW),
        (TicketStatus.DONE, Status.DONE),
        (TicketStatus.BACKLOG, Status.BACKLOG),
        (TicketStatus.CANCELED, Status.DONE),
    ],
)
async def test_issues_tracker_maps_ticket_status_onto_the_board(druks_db, asked, board):
    ticket = await _open_ticket()

    async with IssuesTracker() as tracker:
        await tracker.set_status(ticket.identifier, asked)

    assert (await Ticket.get_for_identifier(ticket.identifier)).status == board


async def test_issues_tracker_raises_for_an_unknown_key(druks_db):
    with pytest.raises(UnknownTicketError, match="NOPE-1"):
        await IssuesTracker().set_status("NOPE-1", TicketStatus.IN_PROGRESS)


@pytest.mark.parametrize(
    ("asked", "board"),
    [
        (TicketStatus.IN_PROGRESS, Status.IN_PROGRESS),
        (TicketStatus.IN_REVIEW, Status.IN_REVIEW),
        (TicketStatus.DONE, Status.DONE),
        (TicketStatus.BACKLOG, Status.BACKLOG),
    ],
)
async def test_work_item_status_writes_through_to_the_issues_ticket(
    druks_db, monkeypatch, asked, board
):
    _pin_software_factory_settings(monkeypatch, tracker="issues")
    ticket = await _open_ticket()
    item = await make_test_work_item(
        repo="acme/widget", source="issues", ticket_key=ticket.identifier, title="one"
    )

    await item.set_ticket_status(asked)

    assert (await Ticket.get_for_identifier(ticket.identifier)).status == board


async def test_ready_for_agent_opens_a_build_against_the_selected_repo(druks_db, monkeypatch):
    await _connect_github()
    _pin_software_factory_settings(monkeypatch, tracker="issues")
    ticket = await _open_ticket(title="Add an endpoint")
    started = []

    async def fake_start(cls, **kwargs):
        started.append(kwargs)
        return "run-1"

    monkeypatch.setattr(Build, "start", classmethod(fake_start))

    await ticket.transition(Status.READY_FOR_AGENT)

    item = await WorkItem.get_for_ticket_key(source="issues", ticket_key=ticket.identifier)
    assert item.source == "issues"
    assert item.ticket_key == "WID-1"
    assert item.repo == "acme/widget"
    assert started[0]["subject"].id == item.id


async def test_ready_for_agent_on_a_parked_build_moves_the_ticket_to_in_review(
    druks_db, monkeypatch
):
    await _connect_github()
    _pin_software_factory_settings(monkeypatch, tracker="issues")
    ticket = await _open_ticket(title="Add an endpoint")
    item = await make_test_work_item(
        repo="acme/widget",
        source="issues",
        ticket_key=ticket.identifier,
        title=ticket.title,
    )
    await seed_build_run(druks_db, work_item_id=item.id, state="parked", input_gate=ReviewWork.name)
    started = []

    async def fake_start(cls, **kwargs):
        started.append(kwargs)
        return "should-not-run"

    monkeypatch.setattr(Build, "start", classmethod(fake_start))

    await ticket.transition(Status.READY_FOR_AGENT)

    assert started == []
    assert (await Ticket.get_for_identifier(ticket.identifier)).status == Status.IN_REVIEW

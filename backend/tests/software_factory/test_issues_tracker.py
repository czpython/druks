import druks.contrib.software_factory.subscribers  # noqa: F401
import pytest
from druks.contrib.issues.enums import Status
from druks.contrib.issues.models import Project as IssuesProject
from druks.contrib.issues.models import Ticket
from druks.contrib.issues.tracker import IssuesTracker
from druks.contrib.software_factory.app import SoftwareFactory
from druks.contrib.software_factory.models import WorkItem
from druks.contrib.software_factory.ticketing.enums import TicketStatus
from druks.contrib.software_factory.workflows import Build
from druks.core.apis.exceptions import UnknownTicketError
from druks.services.models import ServiceIdentity

from software_factory.factories import make_test_work_item


def _pin_software_factory_settings(monkeypatch, **values):
    settings = SoftwareFactory.Settings(**values)

    async def _settings(cls):
        return settings

    monkeypatch.setattr(SoftwareFactory, "settings", classmethod(_settings))


async def _connect_github() -> None:
    await ServiceIdentity.connect(
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
        (TicketStatus.CANCELED, Status.CANCELLED),
    ],
)
async def test_issues_tracker_maps_ticket_status_onto_the_board(druks_db, asked, board):
    project = await IssuesProject.create(name="widget", prefix="WID")
    ticket = await Ticket.create(project_id=project.id, title="one")

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
    project = await IssuesProject.create(name="widget", prefix="WID")
    ticket = await Ticket.create(project_id=project.id, title="one")
    item = await make_test_work_item(
        repo="acme/widget", source="issues", ticket_key=ticket.identifier, title="one"
    )

    await item.set_ticket_status(asked)

    assert (await Ticket.get_for_identifier(ticket.identifier)).status == board


async def test_ready_for_agent_opens_a_build_when_the_project_names_a_repo(druks_db, monkeypatch):
    await _connect_github()
    _pin_software_factory_settings(monkeypatch, tracker="issues")
    await make_test_work_item(repo="acme/widget", title="seed", ticket_key="SEED-1")
    project = await IssuesProject.create(name="widget", prefix="WID")
    ticket = await Ticket.create(project_id=project.id, title="Add an endpoint")
    started = []

    async def fake_start(cls, **kwargs):
        started.append(kwargs)
        return "run-1"

    monkeypatch.setattr(Build, "start", classmethod(fake_start))

    await ticket.transition(Status.READY_FOR_AGENT)

    item = await WorkItem.get_for_ticket_key(source="issues", ticket_key=ticket.identifier)
    assert item.source == "issues"
    assert item.ticket_key == "WID-1"
    assert started[0]["subject"].id == item.id


async def test_ready_for_agent_skips_when_the_project_names_no_repo(druks_db, monkeypatch, caplog):
    _pin_software_factory_settings(monkeypatch, tracker="issues")
    project = await IssuesProject.create(name="no-such-repo", prefix="NSR")
    ticket = await Ticket.create(project_id=project.id, title="orphan")
    started = []

    async def fake_start(cls, **kwargs):
        started.append(kwargs)
        return "run-x"

    monkeypatch.setattr(Build, "start", classmethod(fake_start))

    with caplog.at_level("INFO"):
        await ticket.transition(Status.READY_FOR_AGENT)

    assert started == []
    assert await WorkItem.get_for_ticket_key(source="issues", ticket_key=ticket.identifier) is None
    assert any("no routable repo" in record.getMessage() for record in caplog.records)

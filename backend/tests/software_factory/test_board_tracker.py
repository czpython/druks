import druks.contrib.software_factory.subscribers  # noqa: F401
import pytest
from conftest import connect_service
from druks.contrib.software_factory.app import SoftwareFactory
from druks.contrib.software_factory.contracts import ReviewWork
from druks.contrib.software_factory.enums import Status
from druks.contrib.software_factory.models import Project, ProjectRepo, Ticket, WorkItem
from druks.contrib.software_factory.ticketing.druks import DruksTracker
from druks.contrib.software_factory.ticketing.enums import TicketStatus
from druks.contrib.software_factory.workflows import Build
from druks.core.apis.exceptions import UnknownTicketError

from software_factory.factories import make_test_work_item, seed_build_run


async def _open_ticket(*, project="Acme", full_name="acme/widget", title="one"):
    row = await Project.create(name=project)
    repo = await ProjectRepo.create(project_id=row.id, full_name=full_name)
    return await Ticket.create(repo=repo, title=title)


def _pin_tracker(monkeypatch, tracker="druks"):
    settings = SoftwareFactory.Settings(tracker=tracker)

    async def _settings(cls):
        return settings

    monkeypatch.setattr(SoftwareFactory, "settings", classmethod(_settings))


async def _connect_github() -> None:
    await connect_service(
        "github",
        identity={"app_id": "1", "slug": "druks-operator"},
        secrets={"private_key": "operator-pem", "webhook_secret": "hook-secret"},
    )


def _no_start(monkeypatch) -> list[dict]:
    started: list[dict] = []

    async def fake_start(cls, **kwargs):
        started.append(kwargs)
        return "run-1"

    monkeypatch.setattr(Build, "start", classmethod(fake_start))
    return started


@pytest.mark.parametrize(
    ("asked", "board"),
    [
        (TicketStatus.TRIGGER, Status.READY_FOR_AGENT),
        (TicketStatus.BACKLOG, Status.BACKLOG),
        (TicketStatus.IN_PROGRESS, Status.IN_PROGRESS),
        (TicketStatus.IN_REVIEW, Status.IN_REVIEW),
        (TicketStatus.DONE, Status.DONE),
        (TicketStatus.CANCELED, Status.DONE),
    ],
)
async def test_the_tracker_maps_every_asked_status_onto_the_board(druks_db, asked, board):
    ticket = await _open_ticket()

    async with DruksTracker() as tracker:
        await tracker.set_status(ticket.identifier, asked)

    assert (await Ticket.get_for_identifier(ticket.identifier)).status == board


async def test_the_tracker_raises_for_a_ticket_it_does_not_hold(druks_db):
    with pytest.raises(UnknownTicketError, match="NOPE-1"):
        await DruksTracker().set_status("NOPE-1", TicketStatus.IN_PROGRESS)


async def test_a_work_item_writes_its_status_through_to_the_ticket(druks_db, monkeypatch):
    _pin_tracker(monkeypatch)
    ticket = await _open_ticket()
    item = await make_test_work_item(
        repo="acme/widget", source="druks", ticket_key=ticket.identifier, title="one"
    )

    await item.set_ticket_status(TicketStatus.IN_REVIEW)

    assert (await Ticket.get_for_identifier(ticket.identifier)).status == Status.IN_REVIEW


async def test_ready_for_agent_opens_a_build_against_the_ticket_repo(druks_db, monkeypatch):
    await _connect_github()
    _pin_tracker(monkeypatch)
    # The same bare repo name in another org: the build takes the ticket's own repo.
    await _open_ticket(project="Acme", full_name="acme/widget")
    ticket = await _open_ticket(project="Beta", full_name="beta/widget", title="Add an endpoint")
    started = _no_start(monkeypatch)

    await ticket.transition(Status.READY_FOR_AGENT)

    item = await WorkItem.get_for_ticket_key(source="druks", ticket_key=ticket.identifier)
    assert (item.source, item.ticket_key, item.repo) == ("druks", "BET-1", "beta/widget")
    assert started[0]["subject"].id == item.id


async def test_ready_for_agent_on_a_parked_build_moves_the_ticket_to_in_review(
    druks_db, monkeypatch
):
    await _connect_github()
    _pin_tracker(monkeypatch)
    ticket = await _open_ticket(title="Add an endpoint")
    item = await make_test_work_item(
        repo="acme/widget",
        source="druks",
        ticket_key=ticket.identifier,
        title=ticket.title,
    )
    await seed_build_run(druks_db, work_item_id=item.id, state="parked", input_gate=ReviewWork.name)
    started = _no_start(monkeypatch)

    await ticket.transition(Status.READY_FOR_AGENT)

    assert started == []
    assert (await Ticket.get_for_identifier(ticket.identifier)).status == Status.IN_REVIEW

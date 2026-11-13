from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from conftest import connect_service
from druks.contrib.software_factory import subscribers  # noqa: F401 (the import registers them)
from druks.contrib.software_factory.app import SoftwareFactory
from druks.contrib.software_factory.contracts import ReviewWork
from druks.contrib.software_factory.enums import Resolution
from druks.contrib.software_factory.exceptions import (
    TicketAlreadyMerged,
    TicketBlocked,
    TicketNotRoutable,
)
from druks.contrib.software_factory.ticketing.base import Tracker
from druks.contrib.software_factory.workflows import Build
from druks.events.models import Event
from druks.services.exceptions import ServiceNotConnectedError
from druks.signals import publish
from druks.testing import seed_run
from sqlalchemy import select

from software_factory.factories import make_test_work_item, seed_build_run


async def _connect_github() -> None:
    await connect_service(
        "github",
        identity={"app_id": "1", "slug": "druks-operator"},
        secrets={"private_key": "operator-pem", "webhook_secret": "hook-secret"},
    )


def _ticket(item, **overrides) -> dict:
    ticket = {
        "source": item.source,
        "identifier": item.ticket_key,
        "status": "Ready",
        "title": item.title,
        "url": f"https://tracker.test/{item.ticket_key}",
        "project_name": "r",
        "labels": [],
        "actor_id": None,
        "assignee_id": None,
        "assignee_email": None,
        "assignee_name": None,
    }
    ticket.update(overrides)
    return ticket


async def test_dispatch_leaves_the_item_alone(druks_db, monkeypatch) -> None:
    """Dispatch starts the build and changes nothing else. The scheduled reaction
    clears the previous attempt (test_lane_reactions)."""
    await _connect_github()
    await seed_run(druks_db, kind=Build.kind, run_id="run-old")
    await seed_run(druks_db, kind=Build.kind, run_id="run-new")
    item = await make_test_work_item(repo="o/r", title="t", ticket_key="ACME-2")
    await item.update(pr_number=7, branch="agent/old")
    await item.resolve(Resolution.CLOSED, at=datetime.now(UTC))

    async def fake_start(cls, **kwargs):
        return "run-new"

    monkeypatch.setattr(Build, "start", classmethod(fake_start))
    await Build.dispatch(ticket=_ticket(item))

    assert item.pr_number == 7
    assert item.branch == "agent/old"
    assert item.resolution == "closed"


async def test_dispatch_announces_and_refuses_without_github(druks_db, monkeypatch) -> None:
    """Without the GitHub identity, dispatch announces the refusal and starts nothing."""
    item = await make_test_work_item(repo="o/r", title="t", ticket_key="ACME-8")
    started = []

    async def fake_start(cls, **kwargs):
        started.append(kwargs)
        return "run-x"

    monkeypatch.setattr(Build, "start", classmethod(fake_start))

    with pytest.raises(ServiceNotConnectedError):
        await Build.dispatch(ticket=_ticket(item))

    assert not started
    events = list(await druks_db.scalars(select(Event)))
    assert len(events) == 1
    assert events[0].type == "build.rejected"
    assert events[0].app == "software_factory"
    assert events[0].subject_id == str(item.id)
    assert "not connected" in events[0].payload["reason"]
    assert "run" not in events[0].payload


async def test_the_tracker_funnel_swallows_the_missing_identity(druks_db, monkeypatch) -> None:
    """End to end through publish: a ticket transition on an unconnected appliance
    raises nothing, so the webhook never goes into redelivery."""
    settings = await SoftwareFactory.settings()
    item = await make_test_work_item(
        repo="o/r", title="t", ticket_key="ACME-9", source=settings.tracker
    )
    started = []

    async def fake_start(cls, **kwargs):
        started.append(kwargs)
        return "run-x"

    monkeypatch.setattr(Build, "start", classmethod(fake_start))

    await publish(
        "ticket.transitioned",
        payload=_ticket(item, source=settings.tracker, status=settings.trigger_status),
    )

    assert not started


async def test_dispatch_refuses_a_merged_ticket_before_the_identity_check(
    druks_db, monkeypatch
) -> None:
    """A merged item refuses a new build before the identity check runs."""
    item = await make_test_work_item(repo="o/r", title="t", ticket_key="ACME-10")
    await item.update(pr_number=7, branch="agent/old")
    await item.resolve(Resolution.MERGED, at=datetime.now(UTC))

    async def fake_start(cls, **kwargs):
        raise AssertionError("a merged item never starts")

    monkeypatch.setattr(Build, "start", classmethod(fake_start))

    with pytest.raises(TicketAlreadyMerged):
        await Build.dispatch(ticket=_ticket(item))


async def test_dispatch_syncs_a_parked_build_instead_of_restarting(druks_db, monkeypatch) -> None:
    await _connect_github()
    item = await make_test_work_item(repo="o/r", title="t", ticket_key="ACME-12")
    run = await seed_build_run(
        druks_db, work_item_id=item.id, state="parked", input_gate=ReviewWork.name
    )
    started = []

    async def fake_start(cls, **kwargs):
        started.append(kwargs)
        return "should-not-run"

    monkeypatch.setattr(Build, "start", classmethod(fake_start))

    assert await Build.dispatch(ticket=_ticket(item)) == run.id
    assert not started


async def test_dispatch_refuses_an_unroutable_ticket_before_the_identity_check(
    druks_db, monkeypatch
) -> None:
    started = []

    async def fake_start(cls, **kwargs):
        started.append(kwargs)

    monkeypatch.setattr(Build, "start", classmethod(fake_start))

    with pytest.raises(TicketNotRoutable):
        await Build.dispatch(
            ticket={
                "source": "linear",
                "identifier": "ACME-11",
                "status": "Ready",
                "title": "t",
                "url": "https://tracker.test/ACME-11",
                "project_name": "no-such-project",
                "labels": [],
                "actor_id": None,
                "assignee_id": None,
                "assignee_email": None,
                "assignee_name": None,
            }
        )

    assert not started


async def test_update_clears_nullable_with_none_and_skips_omitted(druks_db) -> None:
    """update(pr_number=None) clears the column. An omitted branch keeps its value."""
    item = await make_test_work_item(repo="o/r", title="t", ticket_key="ACME-4")
    await item.update(pr_number=9, branch="agent/keep")

    await item.update(pr_number=None)

    assert not item.pr_number
    assert item.branch == "agent/keep"


@pytest.mark.parametrize(
    ("source", "ticket_key", "held", "branch"),
    [
        ("linear", "ACME-5", {"agent/ACME-5", "agent/ACME-5-2"}, "agent/ACME-5-3"),
        ("github", "o/r#42", set(), "agent/issue-42"),
    ],
)
async def test_create_branch_takes_a_name_github_does_not_hold(
    druks_db, monkeypatch, source, ticket_key, held, branch
) -> None:
    item = await make_test_work_item(repo="o/r", title="t", source=source, ticket_key=ticket_key)
    created = []

    async def has_branch(repo, name):
        return name in held

    async def create_branch(repo, name):
        created.append((repo, name))

    async def get_client(repo):
        return SimpleNamespace(has_branch=has_branch, create_branch=create_branch)

    monkeypatch.setattr("druks.contrib.software_factory.models.Github.get_client", get_client)

    await item.create_branch()

    assert item.branch == branch
    assert created == [("o/r", branch)]


class _FakeTracker(Tracker):
    """Tickets that block one another, with no HTTP. ``blocking`` holds the tickets the
    Done ticket blocks, and each of them is blocked by ``blocked_by``."""

    def __init__(self, *, blocked_by: dict[str, bool], blocking: dict[str, dict] | None = None):
        super().__init__(status_names={})
        self.blocked_by = blocked_by
        self.blocking = blocking or {}

    async def get_ticket(self, key):
        return self.blocking[key]

    async def get_blocked_by(self, key):
        return self.blocked_by

    async def get_blocking(self, key):
        return list(self.blocking)

    async def get_status_name(self, key):
        return self.blocking[key]["status"]

    async def set_status(self, key, status):
        return

    async def aclose(self):
        return


@pytest.fixture
def started(monkeypatch) -> list[str]:
    keys = []

    async def start(cls, *, subject, **kwargs):
        keys.append(subject.ticket_key)
        return "run-x"

    monkeypatch.setattr(Build, "start", classmethod(start))
    return keys


def _use_tracker(monkeypatch, tracker: Tracker) -> None:
    async def get_tracker(cls, source=None):
        return tracker

    monkeypatch.setattr(SoftwareFactory, "get_tracker", classmethod(get_tracker))


async def test_dispatch_waits_on_an_open_blocker(druks_db, monkeypatch, started) -> None:
    await _connect_github()
    item = await make_test_work_item(repo="o/r", title="t", ticket_key="ACME-20")
    tracker = _FakeTracker(blocked_by={"ACME-18": True, "ACME-19": False})
    _use_tracker(monkeypatch, tracker)

    with pytest.raises(TicketBlocked, match="ACME-20 is blocked by ACME-18, so"):
        await Build.dispatch(ticket=_ticket(item))

    assert started == []
    [event] = await druks_db.scalars(select(Event))
    assert event.type == "build.waiting"
    assert event.payload["blockers"] == ["ACME-18"]


async def test_a_done_ticket_starts_each_ticket_it_blocks_at_the_trigger_status(
    druks_db, monkeypatch, started
) -> None:
    await _connect_github()
    settings = await SoftwareFactory.settings()
    ready_item = await make_test_work_item(
        repo="o/r", title="t", ticket_key="ACME-23", source=settings.tracker
    )
    resting_item = await make_test_work_item(
        repo="o/r", title="t", ticket_key="ACME-24", source=settings.tracker
    )
    blocked_tickets = {
        "ACME-23": _ticket(ready_item, status=settings.trigger_status),
        "ACME-24": _ticket(resting_item, status=settings.resting_status),
    }
    tracker = _FakeTracker(blocked_by={"ACME-22": False}, blocking=blocked_tickets)
    _use_tracker(monkeypatch, tracker)

    await publish(
        "ticket.transitioned",
        payload=_ticket(ready_item, identifier="ACME-22", status=settings.done_status),
    )

    assert started == ["ACME-23"]


async def test_a_done_ticket_leaves_a_ticket_with_another_open_blocker(
    druks_db, monkeypatch, started
) -> None:
    await _connect_github()
    settings = await SoftwareFactory.settings()
    item = await make_test_work_item(
        repo="o/r", title="t", ticket_key="ACME-26", source=settings.tracker
    )
    tracker = _FakeTracker(
        blocked_by={"ACME-25": False, "ACME-27": True},
        blocking={"ACME-26": _ticket(item, status=settings.trigger_status)},
    )
    _use_tracker(monkeypatch, tracker)

    await publish(
        "ticket.transitioned",
        payload=_ticket(item, identifier="ACME-25", status=settings.done_status),
    )

    assert started == []


async def test_a_blocker_at_another_status_starts_nothing(druks_db, monkeypatch, started) -> None:
    await _connect_github()
    settings = await SoftwareFactory.settings()
    item = await make_test_work_item(
        repo="o/r", title="t", ticket_key="ACME-29", source=settings.tracker
    )
    tracker = _FakeTracker(
        blocked_by={},
        blocking={"ACME-29": _ticket(item, status=settings.trigger_status)},
    )
    _use_tracker(monkeypatch, tracker)

    await publish(
        "ticket.transitioned",
        payload=_ticket(item, identifier="ACME-28", status=settings.in_review_status),
    )

    assert started == []


async def test_dispatch_passes_the_done_blockers_to_the_build(druks_db, monkeypatch) -> None:
    await _connect_github()
    item = await make_test_work_item(repo="o/r", title="t", ticket_key="ACME-32")
    _use_tracker(monkeypatch, _FakeTracker(blocked_by={"ACME-30": False, "ACME-31": False}))
    started = []

    async def fake_start(cls, **kwargs):
        started.append(kwargs)
        return "run-x"

    monkeypatch.setattr(Build, "start", classmethod(fake_start))

    await Build.dispatch(ticket=_ticket(item))

    assert started[0]["blockers"] == ["ACME-30", "ACME-31"]


async def test_each_blocker_gives_its_merged_pr_or_its_key(druks_db) -> None:
    blocker_item = await make_test_work_item(
        repo="o/api", title="Add the endpoint", ticket_key="ACME-33"
    )
    await blocker_item.update(pr_number=12)
    await blocker_item.resolve(Resolution.MERGED, at=datetime.now(UTC))
    build = Build()
    build.input = Build._run_input_model(blockers=("ACME-33", "ACME-34"))
    build.subject = await make_test_work_item(repo="o/r", title="t", ticket_key="ACME-35")

    blockers = await Build._load_blockers.__wrapped__(build)

    assert blockers == [
        {"repo": "o/api", "pr_number": 12, "title": "Add the endpoint"},
        {"ticket_key": "ACME-34", "pr_number": None},
    ]

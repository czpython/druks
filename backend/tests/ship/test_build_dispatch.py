from datetime import UTC, datetime

from druks.contrib.ship.workflows import Build
from druks.services.models import ServiceIdentity
from druks.signals import publish
from druks.testing import seed_run

from ship.factories import make_test_work_item


def _connect_github() -> None:
    ServiceIdentity.connect(
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
        "assignee_email": None,
        "assignee_name": None,
    }
    ticket.update(overrides)
    return ticket


async def test_dispatch_leaves_the_item_alone(druks_db, monkeypatch) -> None:
    """Dispatch starts the build and touches nothing else — clearing the previous
    attempt is the scheduled reaction's (test_lane_reactions), and a duplicate
    dispatch never makes that announcement."""
    _connect_github()
    seed_run(druks_db, kind=Build.kind, run_id="run-old")
    seed_run(druks_db, kind=Build.kind, run_id="run-new")
    item = make_test_work_item(repo="o/r", title="t", ticket_key="ACME-2")
    item.update(pr_number=7, branch="agent/old")
    item.resolve(merged=False, at=datetime.now(UTC))

    async def fake_start(cls, **kwargs):
        return "run-new"

    monkeypatch.setattr(Build, "start", classmethod(fake_start))
    await Build.dispatch(ticket=_ticket(item))

    assert item.pr_number == 7
    assert item.branch == "agent/old"
    assert item.resolution == "closed"


async def test_dispatch_stands_down_without_github_instead_of_raising(
    druks_db, monkeypatch, caplog
) -> None:
    """The tracker delivery already succeeded — a raise here would 5xx the
    webhook into provider redelivery. No identity: log the not-connected
    direction and start nothing."""
    item = make_test_work_item(repo="o/r", title="t", ticket_key="ACME-8")
    started = []

    async def fake_start(cls, **kwargs):
        started.append(kwargs)
        return "run-x"

    monkeypatch.setattr(Build, "start", classmethod(fake_start))

    with caplog.at_level("INFO"):
        result = await Build.dispatch(ticket=_ticket(item))

    assert result is None
    assert not started
    assert any("not connected" in record.getMessage() for record in caplog.records)


async def test_the_tracker_funnel_swallows_the_missing_identity(druks_db, monkeypatch) -> None:
    """End to end through publish: an unconnected appliance must not turn a
    ticket transition into an escaping exception (which the webhook layer
    would 5xx into indefinite redelivery)."""
    from druks.contrib.ship import subscribers  # noqa: F401 — the import registers it
    from druks.contrib.ship.extension import Ship

    settings = Ship.settings()
    item = make_test_work_item(repo="o/r", title="t", ticket_key="ACME-9", source=settings.tracker)
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


async def test_dispatch_merged_noop_still_precedes_the_identity_guard(
    druks_db, monkeypatch, caplog
) -> None:
    """A merged item's redelivery keeps its own no-op — the identity guard only
    decides deliveries that would otherwise start."""
    item = make_test_work_item(repo="o/r", title="t", ticket_key="ACME-10")
    item.update(pr_number=7, branch="agent/old")
    item.resolve(merged=True, at=datetime.now(UTC))

    async def fake_start(cls, **kwargs):
        raise AssertionError("a merged item never starts")

    monkeypatch.setattr(Build, "start", classmethod(fake_start))

    with caplog.at_level("INFO"):
        assert await Build.dispatch(ticket=_ticket(item)) is None

    assert any("already merged" in record.getMessage() for record in caplog.records)


async def test_dispatch_unroutable_noop_still_precedes_the_identity_guard(
    druks_db, monkeypatch, caplog
) -> None:
    started = []

    async def fake_start(cls, **kwargs):
        started.append(kwargs)

    monkeypatch.setattr(Build, "start", classmethod(fake_start))

    with caplog.at_level("INFO"):
        result = await Build.dispatch(
            ticket={
                "source": "linear",
                "identifier": "ACME-11",
                "status": "Ready",
                "title": "t",
                "url": "https://tracker.test/ACME-11",
                "project_name": "no-such-project",
                "labels": [],
                "assignee_email": None,
                "assignee_name": None,
            }
        )

    assert result is None
    assert not started
    assert any("no routable repo" in record.getMessage() for record in caplog.records)


def test_update_clears_nullable_with_none_and_skips_omitted(druks_db) -> None:
    """update() tells a clear from a skip: pr_number=None clears the column,
    while leaving branch out preserves it."""
    item = make_test_work_item(repo="o/r", title="t", ticket_key="ACME-4")
    item.update(pr_number=9, branch="agent/keep")

    item.update(pr_number=None)

    assert item.pr_number is None
    assert item.branch == "agent/keep"

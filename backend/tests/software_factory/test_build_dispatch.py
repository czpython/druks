from datetime import UTC, datetime

from conftest import connect_service
from druks.contrib.software_factory import subscribers  # noqa: F401 (the import registers them)
from druks.contrib.software_factory.app import SoftwareFactory
from druks.contrib.software_factory.contracts import ReviewWork
from druks.contrib.software_factory.workflows import Build
from druks.signals import publish
from druks.testing import seed_run

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
    await item.resolve(merged=False, at=datetime.now(UTC))

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
    """A raise here would 5xx the webhook into provider redelivery. Without the
    GitHub identity, dispatch logs the direction and starts nothing."""
    item = await make_test_work_item(repo="o/r", title="t", ticket_key="ACME-8")
    started = []

    async def fake_start(cls, **kwargs):
        started.append(kwargs)
        return "run-x"

    monkeypatch.setattr(Build, "start", classmethod(fake_start))

    with caplog.at_level("INFO"):
        result = await Build.dispatch(ticket=_ticket(item))

    assert not result
    assert not started
    assert any("not connected" in record.getMessage() for record in caplog.records)


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


async def test_dispatch_merged_noop_still_precedes_the_identity_guard(
    druks_db, monkeypatch, caplog
) -> None:
    """A redelivery for a merged item does nothing, before the identity check runs."""
    item = await make_test_work_item(repo="o/r", title="t", ticket_key="ACME-10")
    await item.update(pr_number=7, branch="agent/old")
    await item.resolve(merged=True, at=datetime.now(UTC))

    async def fake_start(cls, **kwargs):
        raise AssertionError("a merged item never starts")

    monkeypatch.setattr(Build, "start", classmethod(fake_start))

    with caplog.at_level("INFO"):
        assert not await Build.dispatch(ticket=_ticket(item))

    assert any("already merged" in record.getMessage() for record in caplog.records)


async def test_dispatch_syncs_a_parked_build_instead_of_restarting(druks_db, monkeypatch) -> None:
    await _connect_github()
    item = await make_test_work_item(repo="o/r", title="t", ticket_key="ACME-12")
    await seed_build_run(druks_db, work_item_id=item.id, state="parked", input_gate=ReviewWork.name)
    started = []

    async def fake_start(cls, **kwargs):
        started.append(kwargs)
        return "should-not-run"

    monkeypatch.setattr(Build, "start", classmethod(fake_start))

    assert not await Build.dispatch(ticket=_ticket(item))
    assert not started


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
                "assignee_id": None,
                "assignee_email": None,
                "assignee_name": None,
            }
        )

    assert not result
    assert not started
    assert any("no routable repo" in record.getMessage() for record in caplog.records)


async def test_update_clears_nullable_with_none_and_skips_omitted(druks_db) -> None:
    """update(pr_number=None) clears the column. An omitted branch keeps its value."""
    item = await make_test_work_item(repo="o/r", title="t", ticket_key="ACME-4")
    await item.update(pr_number=9, branch="agent/keep")

    await item.update(pr_number=None)

    assert not item.pr_number
    assert item.branch == "agent/keep"

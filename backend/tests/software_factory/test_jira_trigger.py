from unittest.mock import AsyncMock

import druks.contrib.software_factory.subscribers as subs
from conftest import connect_service
from druks.contrib.software_factory.models import Project, ProjectRepo, WorkItem
from druks.contrib.software_factory.workflows import Build
from druks.testing import seed_run

from software_factory.factories import make_test_work_item


def _jira_payload(*, key="IT-12", status="Open", project="acme-app", labels=None):
    return {
        "source": "jira",
        "ticket_id": "10042",
        "identifier": key,
        "status": status,
        "title": "Add an endpoint",
        "url": None,
        "project_name": project,
        "labels": labels or [],
        "actor_id": None,
        "assignee_id": None,
        "assignee_email": "dev@acme.co",
        "assignee_name": "Dev",
    }


def _pin_settings(monkeypatch, **over):
    settings = subs.SoftwareFactory.Settings(**{"tracker": "jira", **over})

    async def _settings(cls):
        return settings

    monkeypatch.setattr(subs.SoftwareFactory, "settings", classmethod(_settings))


async def test_trigger_status_dispatches_build_with_the_webhook_payload(tmp_path, monkeypatch):
    """The build funnel receives the normalized ticket payload without a refetch."""
    _pin_settings(monkeypatch, trigger_status="Ready")
    build = AsyncMock()
    monkeypatch.setattr(subs.Build, "dispatch", build)
    payload = _jira_payload(status="Ready")

    await subs.ticket_transition_drives_the_funnel(payload=payload)

    build.assert_awaited_once_with(ticket=payload)


async def test_the_in_progress_move_a_build_makes_dispatches_nothing(monkeypatch):
    # A build that a chat starts moves its ticket to In Progress, and the tracker's webhook
    # for that move must not open a second build.
    _pin_settings(monkeypatch, trigger_status="Ready", in_progress_status="In Progress")
    build = AsyncMock()
    monkeypatch.setattr(subs.Build, "dispatch", build)

    await subs.ticket_transition_drives_the_funnel(payload=_jira_payload(status="In Progress"))

    build.assert_not_awaited()


async def test_trigger_status_does_not_redispatch_a_merged_item(druks_db, monkeypatch):
    item = await make_test_work_item(
        repo="octo/gadget",
        source="jira",
        ticket_key="IT-12",
        title="Add an endpoint",
    )
    item.resolution = "merged"
    await druks_db.flush()
    _pin_settings(monkeypatch, trigger_status="Ready")
    start = AsyncMock()
    monkeypatch.setattr(subs.Build, "start", start)

    await subs.ticket_transition_drives_the_funnel(payload=_jira_payload(status="Ready"))

    start.assert_not_awaited()


async def test_trigger_status_redispatches_a_closed_item(druks_db, monkeypatch):
    await connect_service(
        "github",
        identity={"app_id": "1", "slug": "druks-operator"},
        secrets={"private_key": "operator-pem", "webhook_secret": "hook-secret"},
    )
    item = await make_test_work_item(
        repo="octo/gadget",
        source="jira",
        ticket_key="IT-12",
        title="Add an endpoint",
    )
    item.resolution = "closed"
    await druks_db.flush()
    _pin_settings(monkeypatch, trigger_status="Ready")
    start = AsyncMock()
    monkeypatch.setattr(subs.Build, "start", start)

    await subs.ticket_transition_drives_the_funnel(payload=_jira_payload(status="Ready"))

    start.assert_awaited_once()


async def test_trigger_status_routes_a_new_ticket_by_label(tmp_path, druks_db, monkeypatch):
    """A new ticket has no work item. Its label names the repo, and the registry routes it."""
    project = await Project.create(name="octo/gadget")
    await ProjectRepo.create(project_id=project.id, full_name="octo/gadget")
    await druks_db.flush()
    _pin_settings(monkeypatch, trigger_status="Ready")
    await seed_run(druks_db, kind=Build.kind, run_id="run-new")

    async def fake_start(cls, **kwargs):
        return "run-new"

    monkeypatch.setattr(subs.Build, "start", classmethod(fake_start))

    await subs.ticket_transition_drives_the_funnel(
        payload=_jira_payload(key="OCTO-1", status="Ready", project="Octo", labels=["Gadget"]),
    )

    item = await WorkItem.get_for_ticket_key(source="jira", ticket_key="OCTO-1")
    assert item.repo == "octo/gadget"
    assert item.project_id == project.id


async def test_trigger_status_ignores_an_unroutable_ticket(tmp_path, druks_db, monkeypatch):
    """A ticket that matches no registered repo starts no build."""
    _pin_settings(monkeypatch, trigger_status="Ready")
    start = AsyncMock()
    monkeypatch.setattr(subs.Build, "start", start)

    await subs.ticket_transition_drives_the_funnel(
        payload=_jira_payload(key="OCTO-2", status="Ready", project="Octo"),
    )

    start.assert_not_called()


async def test_refinement_candidate_status_no_longer_dispatches(tmp_path, monkeypatch):
    _pin_settings(monkeypatch, trigger_status="Ready")
    build = AsyncMock()
    monkeypatch.setattr(subs.Build, "dispatch", build)

    await subs.ticket_transition_drives_the_funnel(payload=_jira_payload(status="Backlog"))

    build.assert_not_called()


async def test_nonchosen_tracker_status_does_not_dispatch(monkeypatch):
    _pin_settings(monkeypatch, tracker="linear", trigger_status="Ready")
    build = AsyncMock()
    monkeypatch.setattr(subs.Build, "dispatch", build)

    await subs.ticket_transition_drives_the_funnel(payload=_jira_payload(status="Ready"))

    build.assert_not_called()


async def test_the_board_triggers_on_ready_for_agent_whatever_the_status_setting(monkeypatch):
    _pin_settings(monkeypatch, tracker="druks", trigger_status="Ready")
    build = AsyncMock()
    monkeypatch.setattr(subs.Build, "dispatch", build)
    payload = {**_jira_payload(status="Ready for Agent"), "source": "druks"}

    await subs.ticket_transition_drives_the_funnel(payload=payload)
    await subs.ticket_transition_drives_the_funnel(payload={**payload, "status": "Ready"})

    build.assert_awaited_once_with(ticket=payload)

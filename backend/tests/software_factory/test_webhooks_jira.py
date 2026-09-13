from types import SimpleNamespace
from unittest.mock import AsyncMock

import druks.contrib.software_factory.subscribers as subs
import pytest
from conftest import connect_service
from druks.contrib.software_factory import webhooks as webhook_module
from druks.contrib.software_factory.models import Project, ProjectRepo, WorkItem
from druks.contrib.software_factory.webhooks import JiraEvents
from druks.contrib.software_factory.workflows import Build
from druks.testing import make_settings, seed_run
from druks.webhooks.router import router as webhooks_router
from fastapi import HTTPException

from software_factory.factories import make_test_work_item


def _provider(tmp_path, *, payload, headers=None):
    events = JiraEvents(
        request=SimpleNamespace(headers=headers or {}),
        kwargs={},
        settings=make_settings(tmp_path),
    )
    events._data_cached = payload
    return events


def _issue(*, key="IT-12", status="Open", project="acme-app", labels=()):
    return {
        "issue": {
            "key": key,
            "fields": {
                "status": {"name": status},
                "project": {"name": project},
                "summary": "Add an endpoint",
                "labels": list(labels),
                "assignee": {"emailAddress": "dev@acme.co", "displayName": "Dev"},
            },
        },
    }


def _jira_payload(*, key="IT-12", status="Open", project="acme-app", labels=None):
    return {
        "source": "jira",
        "identifier": key,
        "status": status,
        "title": "Add an endpoint",
        "url": None,
        "project_name": project,
        "labels": labels or [],
        "assignee_id": None,
        "assignee_email": "dev@acme.co",
        "assignee_name": "Dev",
    }


def test_route_is_unchanged():
    assert f"{webhooks_router.prefix}/{JiraEvents.path}" == "/_external/jira/events/"


async def _connect_jira(*, base_url="https://jira.test/", webhook_secret="s3cret"):
    return await connect_service(
        "jira",
        identity={"base_url": base_url, "email": "a@b.com", "display_name": "druks"},
        secrets={"api_token": "tok", "webhook_secret": webhook_secret},
    )


async def test_rejects_when_not_connected(tmp_path, druks_db):
    events = _provider(tmp_path, payload=_issue())
    with pytest.raises(HTTPException) as exc:
        await events.request_is_authentic()
    assert exc.value.status_code == 401


async def test_authentic_when_token_header_matches(tmp_path, druks_db):
    await _connect_jira()
    events = _provider(
        tmp_path,
        payload=_issue(),
        headers={"x-druks-webhook-token": "s3cret"},
    )
    assert await events.request_is_authentic()


async def test_rejects_when_token_missing_or_wrong(tmp_path, druks_db):
    await _connect_jira()
    events = _provider(
        tmp_path,
        payload=_issue(),
        headers={"x-druks-webhook-token": "nope"},
    )
    with pytest.raises(HTTPException) as exc:
        await events.request_is_authentic()
    assert exc.value.status_code == 401


async def test_body_without_the_issue_envelope_is_rejected(tmp_path):
    with pytest.raises(HTTPException) as error:
        await _provider(tmp_path, payload=_issue()["issue"]).on_issue_event()
    assert error.value.status_code == 400


async def test_emits_normalized_ticket_transition(tmp_path, druks_db, monkeypatch):
    captured = {}

    async def _emit(event_type, **kwargs):
        captured.update({"event": event_type, **kwargs})

    await _connect_jira()
    monkeypatch.setattr(webhook_module, "publish", _emit)
    await _provider(tmp_path, payload=_issue(key="IT-9", status="Ready")).on_issue_event()

    assert captured["event"] == "ticket.transitioned"
    payload = captured["payload"]
    assert payload["source"] == "jira"
    assert payload["identifier"] == "IT-9"
    assert payload["status"] == "Ready"
    assert payload["assignee_email"] == "dev@acme.co"
    assert payload["url"] == "https://jira.test/browse/IT-9"


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


async def test_trigger_status_does_not_redispatch_a_merged_item(druks_db, monkeypatch):
    item = await make_test_work_item(
        repo="octo/alfred",
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
        repo="octo/alfred",
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
    project = await Project.create(name="octo/alfred")
    await ProjectRepo.create(project_id=project.id, full_name="octo/alfred")
    await druks_db.flush()
    _pin_settings(monkeypatch, trigger_status="Ready")
    await seed_run(druks_db, kind=Build.kind, run_id="run-new")

    async def fake_start(cls, **kwargs):
        return "run-new"

    monkeypatch.setattr(subs.Build, "start", classmethod(fake_start))

    await subs.ticket_transition_drives_the_funnel(
        payload=_jira_payload(key="SHRP-1", status="Ready", project="Octo", labels=["Alfred"]),
    )

    item = await WorkItem.get_for_ticket_key(source="jira", ticket_key="SHRP-1")
    assert item.repo == "octo/alfred"
    assert item.project_id == project.id


async def test_trigger_status_ignores_an_unroutable_ticket(tmp_path, druks_db, monkeypatch):
    """A ticket that matches no registered repo starts no build."""
    _pin_settings(monkeypatch, trigger_status="Ready")
    start = AsyncMock()
    monkeypatch.setattr(subs.Build, "start", start)

    await subs.ticket_transition_drives_the_funnel(
        payload=_jira_payload(key="SHRP-2", status="Ready", project="Octo"),
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

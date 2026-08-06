from types import SimpleNamespace
from unittest.mock import AsyncMock

import druks.contrib.ship.subscribers as subs
import pytest
from druks.contrib.ship import webhooks as webhook_module
from druks.contrib.ship.extension import Ship
from druks.contrib.ship.webhooks import JiraEvents
from druks.contrib.ship.workflows import Build
from druks.testing import make_settings, seed_run
from druks.webhooks.router import router as webhooks_router
from fastapi import HTTPException

from ship.factories import make_test_work_item, seed_build_run


def _provider(tmp_path, *, payload, headers=None):
    events = JiraEvents(
        request=SimpleNamespace(headers=headers or {}),
        kwargs={},
        settings=make_settings(tmp_path),
    )
    events._data_cached = payload
    return events


def _issue(*, key="IT-12", status="Open", status_category="new", project="acme-app", labels=()):
    return {
        "issue": {
            "key": key,
            "fields": {
                "status": {"name": status, "statusCategory": {"key": status_category}},
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
        "assignee_email": "dev@acme.co",
        "assignee_name": "Dev",
        "completed": False,
    }


def test_route_is_unchanged():
    assert f"{webhooks_router.prefix}/{JiraEvents.path}" == "/_external/jira/events/"


def test_rejects_when_no_secret_configured(tmp_path, druks_db):
    events = _provider(tmp_path, payload=_issue())
    with pytest.raises(HTTPException) as exc:
        events.request_is_authentic()
    assert exc.value.status_code == 401


def test_authentic_when_token_header_matches(tmp_path, druks_db):
    Ship.override_setting("jira_webhook_secret", "s3cret")
    events = _provider(
        tmp_path,
        payload=_issue(),
        headers={"x-druks-webhook-token": "s3cret"},
    )
    assert events.request_is_authentic()


def test_rejects_when_token_missing_or_wrong(tmp_path, druks_db):
    Ship.override_setting("jira_webhook_secret", "s3cret")
    events = _provider(
        tmp_path,
        payload=_issue(),
        headers={"x-druks-webhook-token": "nope"},
    )
    with pytest.raises(HTTPException) as exc:
        events.request_is_authentic()
    assert exc.value.status_code == 401


async def test_body_without_the_issue_envelope_is_rejected(tmp_path):
    with pytest.raises(HTTPException) as error:
        await _provider(tmp_path, payload=_issue()["issue"]).on_issue_event()
    assert error.value.status_code == 400


async def test_emits_normalized_ticket_transition(tmp_path, druks_db, monkeypatch):
    captured = {}

    async def _emit(event_type, **kwargs):
        captured.update({"event": event_type, **kwargs})

    Ship.override_setting("jira_base_url", "https://jira.test/")
    monkeypatch.setattr(webhook_module, "publish", _emit)
    await _provider(tmp_path, payload=_issue(key="IT-9", status="Ready")).on_issue_event()

    assert captured["event"] == "ticket.transitioned"
    payload = captured["payload"]
    assert payload["source"] == "jira"
    assert payload["identifier"] == "IT-9"
    assert payload["status"] == "Ready"
    assert payload["assignee_email"] == "dev@acme.co"
    assert payload["url"] == "https://jira.test/browse/IT-9"


async def test_done_category_marks_the_transition_terminal(tmp_path, monkeypatch):
    """The "done" statusCategory is Jira's terminal marker."""
    events = []

    async def _emit(event_type, **kwargs):
        events.append((event_type, kwargs["payload"]))

    monkeypatch.setattr(webhook_module, "publish", _emit)
    payload = _issue(key="IT-9", status="Done", status_category="done")
    await _provider(tmp_path, payload=payload).on_issue_event()

    assert [event for event, _ in events] == ["ticket.transitioned"]
    assert events[0][1]["terminal"] is True


async def test_open_category_is_not_terminal(tmp_path, monkeypatch):
    """An in-flight status (any non-"done" category) transitions but isn't terminal."""
    events = []

    async def _emit(event_type, **kwargs):
        events.append((event_type, kwargs["payload"]))

    monkeypatch.setattr(webhook_module, "publish", _emit)
    provider = _provider(
        tmp_path, payload=_issue(status="In Progress", status_category="indeterminate")
    )
    await provider.on_issue_event()

    assert [event for event, _ in events] == ["ticket.transitioned"]
    assert events[0][1]["terminal"] is False


# --- subscriber: build routing ---------------------------------------------


def _pin_settings(monkeypatch, **over):
    settings = subs.Ship.Settings(**{"tracker": "jira", **over})
    monkeypatch.setattr(subs.Ship, "settings", classmethod(lambda cls: settings))


async def test_trigger_status_dispatches_build_with_the_webhook_payload(
    tmp_path, druks_db, monkeypatch
):
    """The build funnel receives the normalized ticket payload without a refetch."""
    _pin_settings(monkeypatch, jira_trigger_status="Ready")
    build = AsyncMock()
    monkeypatch.setattr(subs.Build, "dispatch", build)
    payload = _jira_payload(status="Ready")

    await subs.ticket_transition_drives_the_funnel(payload=payload)

    build.assert_awaited_once_with(ticket=payload)


async def test_trigger_status_does_not_redispatch_a_resolved_item(druks_db, monkeypatch):
    item = make_test_work_item(
        repo="octo/alfred",
        source="jira",
        ticket_key="IT-12",
        title="Add an endpoint",
    )
    item.resolution = "merged"
    druks_db.flush()
    _pin_settings(monkeypatch, jira_trigger_status="Ready")
    build = AsyncMock()
    monkeypatch.setattr(subs.Build, "dispatch", build)

    await subs.ticket_transition_drives_the_funnel(payload=_jira_payload(status="Ready"))

    build.assert_not_awaited()


@pytest.mark.parametrize("state", ["scheduled", "running", "parked"])
async def test_trigger_status_does_not_redispatch_an_active_build(state, druks_db, monkeypatch):
    item = make_test_work_item(
        repo="octo/alfred",
        source="jira",
        ticket_key="IT-12",
        title="Add an endpoint",
    )
    seed_build_run(druks_db, work_item_id=item.id, state=state)
    _pin_settings(monkeypatch, jira_trigger_status="Ready")
    build = AsyncMock()
    monkeypatch.setattr(subs.Build, "dispatch", build)

    await subs.ticket_transition_drives_the_funnel(payload=_jira_payload(status="Ready"))

    build.assert_not_awaited()


async def test_trigger_status_redispatches_after_a_failed_build(druks_db, monkeypatch):
    item = make_test_work_item(
        repo="octo/alfred",
        source="jira",
        ticket_key="IT-12",
        title="Add an endpoint",
    )
    seed_build_run(druks_db, work_item_id=item.id, state="failed", failure="boom")
    _pin_settings(monkeypatch, jira_trigger_status="Ready")
    build = AsyncMock()
    monkeypatch.setattr(subs.Build, "dispatch", build)
    payload = _jira_payload(status="Ready")

    await subs.ticket_transition_drives_the_funnel(payload=payload)

    build.assert_awaited_once_with(ticket=payload)


async def test_trigger_status_routes_a_new_ticket_by_label(tmp_path, druks_db, monkeypatch):
    """No work item yet: the label names the repo, the registry routes it."""
    from druks.contrib.ship.models import Project, ProjectRepo, WorkItem

    project = Project.create(name="octo/alfred")
    ProjectRepo.create(project_id=project.id, full_name="octo/alfred")
    druks_db.flush()
    _pin_settings(monkeypatch, jira_trigger_status="Ready")
    seed_run(druks_db, kind=Build.kind, run_id="run-new")

    async def fake_start(cls, **kwargs):
        return "run-new"

    monkeypatch.setattr(subs.Build, "start", classmethod(fake_start))

    await subs.ticket_transition_drives_the_funnel(
        payload=_jira_payload(key="SHRP-1", status="Ready", project="Octo", labels=["Alfred"]),
    )

    item = WorkItem.get_for_ticket_key(source="jira", ticket_key="SHRP-1")
    assert item.repo == "octo/alfred"
    assert item.project_id == project.id


async def test_trigger_status_ignores_an_unroutable_ticket(tmp_path, druks_db, monkeypatch):
    """No signal matches a registered repo → no build."""
    _pin_settings(monkeypatch, jira_trigger_status="Ready")
    start = AsyncMock()
    monkeypatch.setattr(subs.Build, "start", start)

    await subs.ticket_transition_drives_the_funnel(
        payload=_jira_payload(key="SHRP-2", status="Ready", project="Octo"),
    )

    start.assert_not_called()


async def test_refinement_candidate_status_no_longer_dispatches(tmp_path, monkeypatch):
    _pin_settings(monkeypatch, jira_trigger_status="Ready")
    build = AsyncMock()
    monkeypatch.setattr(subs.Build, "dispatch", build)

    await subs.ticket_transition_drives_the_funnel(payload=_jira_payload(status="Backlog"))

    build.assert_not_called()


async def test_nonchosen_tracker_status_does_not_dispatch(monkeypatch):
    _pin_settings(monkeypatch, tracker="linear", jira_trigger_status="Ready")
    build = AsyncMock()
    monkeypatch.setattr(subs.Build, "dispatch", build)

    await subs.ticket_transition_drives_the_funnel(payload=_jira_payload(status="Ready"))

    build.assert_not_called()

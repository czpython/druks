from types import SimpleNamespace
from unittest.mock import AsyncMock

import druks.build.subscribers as subs
import druks.core.webhooks.jira as jira_mod
import pytest
from conftest import make_settings, seed_run
from druks.core.webhooks.jira import JiraEvents
from fastapi import HTTPException


def _provider(tmp_path, *, payload, headers=None, **settings_over):
    events = JiraEvents(
        request=SimpleNamespace(headers=headers or {}),
        kwargs={},
        settings=make_settings(tmp_path, **settings_over),
    )
    events._data_cached = payload
    return events


def _issue(*, key="IT-12", status="Open", status_category=None, project="acme-app"):
    category = {"statusCategory": {"key": status_category}} if status_category else {}
    return {
        "issue": {
            "key": key,
            "fields": {
                "status": {"name": status, **category},
                "project": {"name": project},
                "summary": "Add an endpoint",
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


# --- provider: auth + parse/emit -------------------------------------------


def test_rejects_when_no_secret_configured(tmp_path):
    events = _provider(tmp_path, payload=_issue(), jira_webhook_secret="")
    with pytest.raises(HTTPException) as exc:
        events.request_is_authentic()
    assert exc.value.status_code == 401


def test_authentic_when_token_header_matches(tmp_path):
    events = _provider(
        tmp_path,
        payload=_issue(),
        headers={"x-druks-webhook-token": "s3cret"},
        jira_webhook_secret="s3cret",
    )
    assert events.request_is_authentic()


def test_rejects_when_token_missing_or_wrong(tmp_path):
    events = _provider(
        tmp_path,
        payload=_issue(),
        headers={"x-druks-webhook-token": "nope"},
        jira_webhook_secret="s3cret",
    )
    with pytest.raises(HTTPException) as exc:
        events.request_is_authentic()
    assert exc.value.status_code == 401


def test_issue_unwraps_both_envelope_shapes(tmp_path):
    assert _provider(tmp_path, payload=_issue()).issue["key"] == "IT-12"
    assert _provider(tmp_path, payload=_issue()["issue"]).issue["key"] == "IT-12"


async def test_emits_normalized_ticket_transition(tmp_path, monkeypatch):
    captured = {}

    async def _emit(event_type, **kwargs):
        captured.update({"event": event_type, **kwargs})

    monkeypatch.setattr(jira_mod, "publish", _emit)
    await _provider(tmp_path, payload=_issue(key="IT-9", status="Ready")).on_issue_event()

    assert captured["event"] == "ticket.transitioned"
    payload = captured["payload"]
    assert payload["source"] == "jira"
    assert payload["identifier"] == "IT-9"
    assert payload["status"] == "Ready"
    assert payload["assignee_email"] == "dev@acme.co"


async def test_missing_key_does_not_emit(tmp_path, monkeypatch):
    send = AsyncMock()
    monkeypatch.setattr(jira_mod, "publish", send)
    await _provider(tmp_path, payload={"issue": {"fields": {}}}).on_issue_event()
    send.assert_not_called()


async def test_done_category_marks_the_transition_terminal(tmp_path, monkeypatch):
    """The "done" statusCategory is Jira's terminal marker — the transition
    carries terminal=True so the scope-cancel subscriber can filter on it."""
    events = []

    async def _emit(event_type, **kwargs):
        events.append((event_type, kwargs["payload"]))

    monkeypatch.setattr(jira_mod, "publish", _emit)
    payload = _issue(key="IT-9", status="Done", status_category="done")
    await _provider(tmp_path, payload=payload).on_issue_event()

    assert [event for event, _ in events] == ["ticket.transitioned"]
    assert events[0][1]["terminal"] is True


async def test_open_category_is_not_terminal(tmp_path, monkeypatch):
    """An in-flight status (any non-"done" category) transitions but isn't terminal."""
    events = []

    async def _emit(event_type, **kwargs):
        events.append((event_type, kwargs["payload"]))

    monkeypatch.setattr(jira_mod, "publish", _emit)
    provider = _provider(
        tmp_path, payload=_issue(status="In Progress", status_category="indeterminate")
    )
    await provider.on_issue_event()

    assert [event for event, _ in events] == ["ticket.transitioned"]
    assert events[0][1]["terminal"] is False


# --- subscriber: scope + build routing -------------------------------------


def _pin_settings(monkeypatch, **over):
    settings = subs.Build.Settings(scoper_candidate_statuses=("Backlog", "Todo"), **over)
    monkeypatch.setattr(subs.Build, "settings", classmethod(lambda cls: settings))


async def test_candidate_status_dispatches_scope(tmp_path, monkeypatch):
    """A status in ``scoper_candidate_statuses`` fetches the ticket and scopes."""
    _pin_settings(monkeypatch)
    ticket = object()
    tracker = AsyncMock()
    tracker.fetch_ticket = AsyncMock(return_value=ticket)
    tracker.__aenter__ = AsyncMock(return_value=tracker)
    tracker.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(subs, "get_tracker", lambda _s: tracker)
    scope = AsyncMock()
    monkeypatch.setattr(subs.Scope, "dispatch", scope)

    await subs.ticket_transition_drives_the_funnel(payload=_jira_payload(status="Backlog"))

    tracker.fetch_ticket.assert_awaited_once_with("IT-12")
    scope.assert_awaited_once_with(ticket=ticket)


async def test_trigger_status_dispatches_build_with_the_webhook_payload(tmp_path, monkeypatch):
    """The build funnel receives the normalized ticket payload without a refetch."""
    _pin_settings(monkeypatch, jira_trigger_status="Ready")
    build = AsyncMock()
    monkeypatch.setattr(subs.BuildWorkflow, "dispatch", build)
    payload = _jira_payload(status="Ready")

    await subs.ticket_transition_drives_the_funnel(payload=payload)

    build.assert_awaited_once_with(ticket=payload)


async def test_trigger_status_routes_an_unscoped_ticket_by_label(tmp_path, db_session, monkeypatch):
    """No work item yet: the label names the repo, the registry routes it."""
    from druks.build.models import Project, ProjectRepo, WorkItem

    project = Project.create(name="octo/alfred")
    ProjectRepo.create(project_id=project.id, full_name="octo/alfred")
    db_session.flush()
    _pin_settings(monkeypatch, jira_trigger_status="Ready")
    seed_run(db_session, "run-new")

    async def fake_start(cls, **kwargs):
        return "run-new"

    monkeypatch.setattr(subs.BuildWorkflow, "start", classmethod(fake_start))

    await subs.ticket_transition_drives_the_funnel(
        payload=_jira_payload(key="SHRP-1", status="Ready", project="Octo", labels=["Alfred"]),
    )

    item = WorkItem.get_for_remote_key(source="jira", remote_key="SHRP-1")
    assert item.build_run_id == "run-new"
    assert item.repo == "octo/alfred"
    assert item.project_id == project.id


async def test_trigger_status_ignores_an_unroutable_ticket(tmp_path, db_session, monkeypatch):
    """No signal matches a registered repo → no build."""
    _pin_settings(monkeypatch, jira_trigger_status="Ready")
    start = AsyncMock()
    monkeypatch.setattr(subs.BuildWorkflow, "start", start)

    await subs.ticket_transition_drives_the_funnel(
        payload=_jira_payload(key="SHRP-2", status="Ready", project="Octo"),
    )

    start.assert_not_called()


async def test_irrelevant_status_does_nothing(tmp_path, monkeypatch):
    """A status that's neither candidate nor trigger dispatches neither path."""
    _pin_settings(monkeypatch, jira_trigger_status="Ready")
    scope = AsyncMock()
    build = AsyncMock()
    monkeypatch.setattr(subs.Scope, "dispatch", scope)
    monkeypatch.setattr(subs.BuildWorkflow, "dispatch", build)

    await subs.ticket_transition_drives_the_funnel(payload=_jira_payload(status="In Progress"))

    scope.assert_not_called()
    build.assert_not_called()

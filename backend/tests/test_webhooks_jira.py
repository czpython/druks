from types import SimpleNamespace

import pytest
from conftest import connect_service
from druks.core.webhooks import jira as webhook_module
from druks.core.webhooks.jira import JiraEvents
from druks.testing import make_settings
from druks.webhooks.router import match_webhook
from druks.webhooks.router import router as webhooks_router
from fastapi import HTTPException


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
            "id": "10042",
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


def test_route_is_unchanged():
    assert f"{webhooks_router.prefix}/{JiraEvents.path}" == "/_external/jira/events/"


def test_the_platform_owns_the_route():
    # Any app consumes Jira facts, so the handler cannot live in one of them.
    assert match_webhook("jira/events/")[0].__module__ == "druks.core.webhooks.jira"


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
    await _provider(
        tmp_path,
        payload=_issue(key="IT-9", status="Ready"),
        headers={"x-jira-initiator": "actor-1"},
    ).on_issue_event()

    assert captured["event"] == "ticket.transitioned"
    payload = captured["payload"]
    assert payload["source"] == "jira"
    assert payload["ticket_id"] == "10042"
    assert payload["identifier"] == "IT-9"
    assert payload["status"] == "Ready"
    assert payload["actor_id"] == "actor-1"
    assert payload["assignee_email"] == "dev@acme.co"
    assert payload["url"] == "https://jira.test/browse/IT-9"

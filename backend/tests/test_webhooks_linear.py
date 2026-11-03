import hashlib
import hmac
from types import SimpleNamespace
from typing import Any, cast

import pytest
from conftest import connect_service
from druks.core.webhooks import linear as webhook_module
from druks.core.webhooks.linear import LinearEvents
from druks.testing import make_settings
from druks.webhooks.router import match_webhook
from druks.webhooks.router import router as webhooks_router
from fastapi import HTTPException


def _provider(tmp_path, *, payload, headers=None):
    events = LinearEvents(
        request=cast(Any, SimpleNamespace(headers=headers or {})),
        kwargs={},
        settings=make_settings(tmp_path),
    )
    events._data_cached = payload
    return events


def _transition():
    return {
        "action": "update",
        "type": "Issue",
        "updatedFrom": {"stateId": "old-state"},
        "actor": {"id": "actor-1", "type": "user"},
        "data": {
            "id": "issue-uuid-7",
            "identifier": "ACME-7",
            "title": "Add an endpoint",
            "url": "https://linear.app/acme/issue/ACME-7",
            "teamId": "team-1",
            "state": {"name": "Done"},
            "project": {"name": "acme-app"},
            "labels": [{"id": "label-1", "name": "gadget", "color": "#5e6ad2"}],
            "assignee": {"id": "user-7", "email": "dev@acme.co", "name": "Dev"},
        },
    }


def _capture(monkeypatch):
    events = []

    async def _emit(event_type, **kwargs):
        events.append((event_type, kwargs["payload"]))

    monkeypatch.setattr(webhook_module, "publish", _emit)
    return events


def test_route_is_unchanged():
    assert f"{webhooks_router.prefix}/{LinearEvents.path}" == "/_external/linear/events/"


def test_the_platform_owns_the_route():
    # Any app consumes Linear facts, so the handler cannot live in one of them.
    assert match_webhook("linear/events/")[0].__module__ == "druks.core.webhooks.linear"


async def test_authentication_reads_the_service_row(tmp_path, druks_db):
    secret = "linear-secret"
    raw_body = b"{}"
    signature = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    await connect_service(
        "linear",
        identity={"actor": "druks", "workspace": "Acme"},
        secrets={"api_key": "lin_secret", "webhook_secret": secret},
    )
    events = _provider(
        tmp_path,
        payload={},
        headers={"linear-signature": signature},
    )
    events.raw_body = raw_body

    assert await events.request_is_authentic()


async def test_rejects_when_not_connected(tmp_path, druks_db):
    events = _provider(
        tmp_path,
        payload={},
        headers={"linear-signature": "anything"},
    )
    events.raw_body = b"{}"

    with pytest.raises(HTTPException) as error:
        await events.request_is_authentic()

    assert error.value.status_code == 401
    assert "not connected" in error.value.detail


async def test_emits_normalized_ticket_transition(tmp_path, monkeypatch):
    events = _capture(monkeypatch)

    await _provider(tmp_path, payload=_transition()).on_state_transition()

    assert events == [
        (
            "ticket.transitioned",
            {
                "source": "linear",
                "ticket_id": "issue-uuid-7",
                "identifier": "ACME-7",
                "status": "Done",
                "title": "Add an endpoint",
                "url": "https://linear.app/acme/issue/ACME-7",
                "project_name": "acme-app",
                "labels": ["gadget"],
                "team": "team-1",
                "actor_id": "actor-1",
                "assignee_id": "user-7",
                "assignee_email": "dev@acme.co",
                "assignee_name": "Dev",
            },
        )
    ]


def _issue(*, action, updated_from=None, project=None):
    return {
        "action": action,
        "type": "Issue",
        "updatedFrom": updated_from,
        "actor": {"id": "user-3", "type": "user"},
        "data": {
            "id": "issue-uuid-7",
            "identifier": "ACME-7",
            "title": "Add an endpoint",
            "url": "https://linear.app/acme/issue/ACME-7",
            "teamId": "team-1",
            "project": project,
            "labels": [{"id": "label-1", "name": "gadget", "color": "#5e6ad2"}],
        },
    }


def _ticket(*, project_name=None):
    return {
        "source": "linear",
        "ticket_id": "issue-uuid-7",
        "identifier": "ACME-7",
        "title": "Add an endpoint",
        "url": "https://linear.app/acme/issue/ACME-7",
        "project_name": project_name,
        "labels": ["gadget"],
        "team": "team-1",
        "actor_id": "user-3",
    }


async def test_a_new_issue_publishes_ticket_created(tmp_path, monkeypatch):
    events = _capture(monkeypatch)
    payload = _issue(action="create", project={"id": "p-1", "name": "Intake", "url": "u"})
    provider = _provider(tmp_path, payload=payload)

    assert provider.get_action() == "issue_created"
    await provider.on_issue_created()

    assert events == [("ticket.created", _ticket(project_name="Intake"))]


async def test_a_content_change_publishes_ticket_updated(tmp_path, monkeypatch):
    events = _capture(monkeypatch)
    provider = _provider(tmp_path, payload=_issue(action="update", updated_from={"title": "old"}))

    assert provider.get_action() == "issue_updated"
    await provider.on_issue_updated()

    assert events == [("ticket.updated", _ticket())]


@pytest.mark.parametrize("updated_from", [{"sortOrder": 1.5}, {"priority": 2}, None])
def test_an_update_that_changes_no_content_is_not_a_ticket_update(tmp_path, updated_from):
    provider = _provider(tmp_path, payload=_issue(action="update", updated_from=updated_from))

    assert provider.get_action() == "ticket_change"


def test_a_state_change_stays_a_transition(tmp_path):
    payload = _issue(action="update", updated_from={"stateId": "s-1", "title": "old"})

    assert _provider(tmp_path, payload=payload).get_action() == "state_transition"


async def test_a_comment_publishes_ticket_commented(tmp_path, monkeypatch):
    events = _capture(monkeypatch)
    payload = {
        "action": "create",
        "type": "Comment",
        "actor": {"id": "user-3", "type": "user"},
        "data": {
            "id": "comment-1",
            "issue": {
                "id": "issue-uuid-7",
                "identifier": "ACME-7",
                "title": "Add an endpoint",
                "url": "https://linear.app/acme/issue/ACME-7",
                "teamId": "team-1",
            },
            "parentId": None,
            "body": "Hi",
        },
    }

    await _provider(tmp_path, payload=payload).on_comment_created()

    assert events == [
        (
            "ticket.commented",
            {
                "source": "linear",
                "ticket_id": "issue-uuid-7",
                "identifier": "ACME-7",
                "title": "Add an endpoint",
                "url": "https://linear.app/acme/issue/ACME-7",
                "project_name": None,
                "labels": None,
                "team": "team-1",
                "actor_id": "user-3",
                "comment_id": "comment-1",
                "body": "Hi",
                "parent_id": None,
            },
        )
    ]


async def test_a_comment_on_no_issue_is_no_ticket_comment(tmp_path, monkeypatch):
    events = _capture(monkeypatch)
    payload = {
        "action": "create",
        "type": "Comment",
        "data": {"id": "comment-1", "projectUpdateId": "update-1", "body": "Hi"},
    }

    await _provider(tmp_path, payload=payload).on_comment_created()

    assert events == []

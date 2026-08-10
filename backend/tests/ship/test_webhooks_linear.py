import hashlib
import hmac
from types import SimpleNamespace
from typing import Any, cast

import pytest
from druks.contrib.ship import webhooks as webhook_module
from druks.contrib.ship.webhooks import LinearEvents
from druks.services.models import ServiceIdentity
from druks.testing import make_settings
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


def _transition(*, identifier="ACME-7", state_name="Done", state_type="completed"):
    return {
        "action": "update",
        "type": "Issue",
        "updatedFrom": {"stateId": "old-state"},
        "data": {
            "identifier": identifier,
            "title": "Add an endpoint",
            "url": f"https://linear.app/acme/issue/{identifier}",
            "state": {"name": state_name, "type": state_type},
            "project": {"name": "acme-app"},
            "assignee": {"email": "dev@acme.co", "name": "Dev"},
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


def test_authentication_reads_the_service_row(tmp_path, druks_db):
    secret = "linear-secret"
    raw_body = b"{}"
    signature = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    ServiceIdentity.connect(
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

    assert events.request_is_authentic()


def test_rejects_when_not_connected(tmp_path, druks_db):
    events = _provider(
        tmp_path,
        payload={},
        headers={"linear-signature": "anything"},
    )
    events.raw_body = b"{}"

    with pytest.raises(HTTPException) as error:
        events.request_is_authentic()

    assert error.value.status_code == 401
    assert "not connected" in error.value.detail


async def test_terminal_state_types_mark_the_transition_terminal(tmp_path, monkeypatch):
    """Completed and canceled states mark the normalized transition terminal."""
    for state_type, name in (("completed", "Done"), ("canceled", "Cancelled")):
        events = _capture(monkeypatch)
        provider = _provider(tmp_path, payload=_transition(state_name=name, state_type=state_type))
        await provider.on_state_transition()

        assert [event for event, _ in events] == ["ticket.transitioned"]
        assert events[0][1]["terminal"] is True


async def test_open_state_types_are_not_terminal(tmp_path, monkeypatch):
    """An in-flight state (started here) transitions but isn't terminal."""
    events = _capture(monkeypatch)
    provider = _provider(
        tmp_path, payload=_transition(state_name="In Progress", state_type="started")
    )
    await provider.on_state_transition()

    assert [event for event, _ in events] == ["ticket.transitioned"]
    assert events[0][1]["terminal"] is False

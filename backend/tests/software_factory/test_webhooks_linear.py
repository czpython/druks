import hashlib
import hmac
from types import SimpleNamespace
from typing import Any, cast

import pytest
from conftest import connect_service
from druks.contrib.software_factory import webhooks as webhook_module
from druks.contrib.software_factory.webhooks import LinearEvents
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


def _transition():
    return {
        "action": "update",
        "type": "Issue",
        "updatedFrom": {"stateId": "old-state"},
        "data": {
            "identifier": "ACME-7",
            "title": "Add an endpoint",
            "url": "https://linear.app/acme/issue/ACME-7",
            "state": {"name": "Done"},
            "project": {"name": "acme-app"},
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
                "identifier": "ACME-7",
                "status": "Done",
                "title": "Add an endpoint",
                "url": "https://linear.app/acme/issue/ACME-7",
                "project_name": "acme-app",
                "labels": [],
                "assignee_id": "user-7",
                "assignee_email": "dev@acme.co",
                "assignee_name": "Dev",
            },
        )
    ]

from types import SimpleNamespace
from typing import Any, cast

import druks.core.webhooks.linear as linear_mod
from druks.core.webhooks.linear import LinearEvents
from druks.testing import make_settings


def _provider(tmp_path, *, payload):
    events = LinearEvents(
        request=cast(Any, SimpleNamespace(headers={})),
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

    monkeypatch.setattr(linear_mod, "publish", _emit)
    return events


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

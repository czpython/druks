import json

import httpx
import pytest
from druks.contrib.ship import services
from druks.contrib.ship.extension import Ship, check_tracker_identity
from druks.contrib.ship.ticketing.enums import TicketStatus
from druks.contrib.ship.ticketing.exceptions import (
    JiraAPIError,
    LinearAPIError,
    UnknownTicketError,
)
from druks.contrib.ship.ticketing.jira import Jira, JiraClient
from druks.contrib.ship.ticketing.linear import Linear, LinearClient
from druks.services import ServiceConnectError
from druks.services.models import ServiceIdentity

from ship.factories import make_test_work_item


def _pin_ship_settings(monkeypatch, **values):
    settings = Ship.Settings(**values)
    monkeypatch.setattr(Ship, "settings", classmethod(lambda cls: settings))


def _connect_linear():
    return ServiceIdentity.connect(
        "linear",
        identity={"actor": "druks", "workspace": "Acme"},
        secrets={"api_key": "lin_secret", "webhook_secret": "lin-hook"},
    )


def _connect_jira():
    return ServiceIdentity.connect(
        "jira",
        identity={"base_url": "https://jira.test", "email": "a@b.com", "display_name": "druks"},
        secrets={"api_token": "jira_secret", "webhook_secret": "jira-hook"},
    )


# --- Ship.get_tracker: the selected tracker ----------------------------------


def test_tracker_builds_linear_from_the_service_row(druks_db, monkeypatch):
    _connect_linear()
    _pin_ship_settings(
        monkeypatch,
        linear_resting_status="Backlog",
        linear_trigger_status="To Agent",
    )

    tracker = Ship.get_tracker("linear")

    assert isinstance(tracker, Linear)
    assert tracker._client.api_key == "lin_secret"
    assert tracker._status_names[TicketStatus.BACKLOG] == "Backlog"
    assert tracker._status_names[TicketStatus.TRIGGER] == "To Agent"


def test_tracker_builds_jira_from_the_service_row(druks_db, monkeypatch):
    _connect_jira()
    _pin_ship_settings(
        monkeypatch,
        tracker="jira",
        jira_resting_status="Open",
        jira_trigger_status="To Agent",
    )

    tracker = Ship.get_tracker("jira")

    assert isinstance(tracker, Jira)
    assert tracker._client.base_url == "https://jira.test"
    assert tracker._status_names[TicketStatus.BACKLOG] == "Open"
    assert tracker._status_names[TicketStatus.TRIGGER] == "To Agent"


def test_tracker_is_none_for_github_and_a_disconnected_identity(druks_db, monkeypatch):
    _connect_linear()
    _pin_ship_settings(monkeypatch)

    assert not Ship.get_tracker("github")
    assert not Ship.get_tracker("jira")

    _pin_ship_settings(monkeypatch, tracker="jira")
    assert not Ship.get_tracker("jira")
    assert not Ship.get_tracker("linear")


def test_tracker_ignores_a_nonchosen_source_with_a_connected_identity(druks_db, monkeypatch):
    _connect_linear()
    _pin_ship_settings(monkeypatch, tracker="jira")

    assert not Ship.get_tracker("linear")


def test_empty_resting_status_leaves_backlog_unmapped(druks_db, monkeypatch):
    _connect_linear()
    _pin_ship_settings(monkeypatch, linear_resting_status="")

    tracker = Ship.get_tracker("linear")

    assert TicketStatus.BACKLOG not in tracker._status_names


# --- The tracker service identities ------------------------------------------


async def test_linear_verify_stores_the_actor_and_workspace(monkeypatch):
    async def fake_post(self, url, **kwargs):
        return httpx.Response(
            200,
            json={"data": {"viewer": {"name": "druks"}, "organization": {"name": "Acme"}}},
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    facts = await services.Linear.verify(services.Linear.Settings(api_key="k", webhook_secret="s"))

    assert facts == {"actor": "druks", "workspace": "Acme"}


async def test_linear_verify_rejects_a_bad_key_without_echoing(monkeypatch):
    async def fake_post(self, url, **kwargs):
        return httpx.Response(400, json={"errors": [{"message": "auth boom-marker"}]})

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    with pytest.raises(ServiceConnectError) as raised:
        await services.Linear.verify(services.Linear.Settings(api_key="bad", webhook_secret="s"))

    assert "boom-marker" not in str(raised.value)
    assert "bad" not in str(raised.value)


async def test_jira_verify_stores_the_display_name(monkeypatch):
    seen = []

    async def fake_get(self, url, **kwargs):
        seen.append(url)
        return httpx.Response(200, json={"displayName": "Druks Bot"})

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

    facts = await services.Jira.verify(
        services.Jira.Settings(
            base_url="https://jira.test/", email="a@b.com", api_token="t", webhook_secret="s"
        )
    )

    assert facts == {"display_name": "Druks Bot"}
    assert seen == ["https://jira.test/rest/api/3/myself"]


async def test_jira_verify_rejects_bad_credentials(monkeypatch):
    async def fake_get(self, url, **kwargs):
        return httpx.Response(401, text="nope")

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

    with pytest.raises(ServiceConnectError, match="did not accept"):
        await services.Jira.verify(
            services.Jira.Settings(
                base_url="https://jira.test", email="a@b.com", api_token="bad", webhook_secret="s"
            )
        )


# --- The tracker doctor check -------------------------------------------------


def test_tracker_check_accepts_trackerless_by_choice(monkeypatch):
    _pin_ship_settings(monkeypatch, tracker="none")

    result = check_tracker_identity()

    assert result.ok
    assert "choice" in result.detail


def test_tracker_check_reports_a_selected_connected_tracker(druks_db, monkeypatch):
    _connect_linear()
    _pin_ship_settings(monkeypatch)

    result = check_tracker_identity()

    assert result.ok
    assert "linear" in result.detail


def test_tracker_check_pends_a_selected_unconnected_tracker(druks_db, monkeypatch):
    _pin_ship_settings(monkeypatch, tracker="jira")

    result = check_tracker_identity()

    assert not result.ok
    assert result.pending
    assert "jira" in result.detail


# --- Linear provider --------------------------------------------------------


class _FakeLinearClient:
    """Records the client calls the provider delegates to — no HTTP."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def update_issue_status(self, issue_id, status_name):
        self.calls.append(("update_issue_status", issue_id, status_name))

    async def aclose(self):
        self.calls.append(("aclose",))


@pytest.mark.asyncio
async def test_set_status_maps_the_ticket_status_to_a_provider_name():
    fake = _FakeLinearClient()
    provider = Linear(
        api_key="lin_x",
        backlog_status="Backlog",
        trigger_status="To Agent",
        client=object(),
    )
    provider._client = fake  # the unit seam is the API client, not HTTP
    await provider.set_status("ACME-270", TicketStatus.DONE)
    await provider.set_status("ACME-270", TicketStatus.BACKLOG)
    await provider.set_status("ACME-270", TicketStatus.TRIGGER)
    assert fake.calls == [
        ("update_issue_status", "ACME-270", "Done"),
        ("update_issue_status", "ACME-270", "Backlog"),
        ("update_issue_status", "ACME-270", "To Agent"),
    ]


@pytest.mark.asyncio
async def test_set_status_unmapped_raises():
    provider = Linear(api_key="lin_x", client=object())
    provider._client = _FakeLinearClient()
    with pytest.raises(ValueError, match="no configured status"):
        await provider.set_status("ACME-270", TicketStatus.BACKLOG)
    with pytest.raises(ValueError, match="no configured status"):
        await provider.set_status("ACME-270", TicketStatus.TRIGGER)


def test_linear_declares_known_exceptions():
    assert LinearAPIError in Linear.known_exceptions
    assert UnknownTicketError in Linear.known_exceptions
    assert httpx.HTTPError in Linear.known_exceptions


# --- LinearClient: the GraphQL request sequences ----------------------------


def _linear_client(handler) -> LinearClient:
    wire = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return LinearClient(api_key="lin_x", client=wire)


def _linear_issue_response(*, current: str, states: list[tuple[str, str]]) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "data": {
                "issue": {
                    "id": "issue-uuid",
                    "identifier": "ENG-831",
                    "state": {"id": "current-id", "name": current},
                    "team": {
                        "states": {"nodes": [{"id": id_, "name": name} for id_, name in states]}
                    },
                }
            }
        },
    )


@pytest.mark.asyncio
async def test_linear_client_skips_the_mutation_when_already_at_the_status():
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return _linear_issue_response(
            current="Ready for Agent", states=[("current-id", "Ready for Agent")]
        )

    result = await _linear_client(handler).update_issue_status("ENG-831", "Ready for Agent")

    assert result == {"identifier": "ENG-831", "status": "Ready for Agent", "changed": False}
    # Equal state is a successful no-op: the lookup query, no update mutation.
    assert len(requests) == 1
    assert "mutation" not in requests[0]["query"]


@pytest.mark.asyncio
async def test_linear_client_mutates_a_ticket_not_at_the_status():
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append(body)
        if "mutation" in body["query"]:
            return httpx.Response(
                200,
                json={
                    "data": {
                        "issueUpdate": {
                            "success": True,
                            "issue": {
                                "identifier": "ENG-831",
                                "state": {"name": "Ready for Agent"},
                            },
                        }
                    }
                },
            )
        return _linear_issue_response(
            current="Backlog",
            states=[("current-id", "Backlog"), ("target-id", "Ready for Agent")],
        )

    result = await _linear_client(handler).update_issue_status("ENG-831", "Ready for Agent")

    assert result == {"identifier": "ENG-831", "status": "Ready for Agent", "changed": True}
    assert ["mutation" in body["query"] for body in requests] == [False, True]
    assert requests[1]["variables"] == {"issueId": "ENG-831", "statusId": "target-id"}


@pytest.mark.asyncio
async def test_linear_client_translates_a_null_issue_to_unknown_ticket():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"issue": None}})

    with pytest.raises(UnknownTicketError, match="ENG-9999 doesn't exist in Linear"):
        await _linear_client(handler).update_issue_status("ENG-9999", "Ready for Agent")


# --- WorkItem.set_ticket_status: the status-push consumer -------------------


class _FakeTracker:
    known_exceptions: tuple = ()

    def __init__(self) -> None:
        self.calls: list = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.aclose()

    async def set_status(self, key, status):
        self.calls.append((key, status))

    async def aclose(self):
        self.calls.append("aclose")


@pytest.mark.asyncio
async def test_ticket_state_pushes_status(druks_db, monkeypatch):
    item = make_test_work_item(repo="acme/widget", source="linear", ticket_key="ACME-1", title="t")
    fake = _FakeTracker()
    monkeypatch.setattr(Ship, "get_tracker", classmethod(lambda cls, source=None: fake))

    await item.set_ticket_status(TicketStatus.DONE)

    assert fake.calls == [("ACME-1", TicketStatus.DONE), "aclose"]


@pytest.mark.asyncio
async def test_ticket_state_skips_non_tracker_source(druks_db):
    item = make_test_work_item(repo="acme/widget", source="github", ticket_key="#5", title="t")
    # github has no tracker — a no-op that must not raise.
    await item.set_ticket_status(TicketStatus.DONE)


@pytest.mark.asyncio
async def test_ticket_state_closes_on_failure(druks_db, monkeypatch):
    item = make_test_work_item(repo="acme/widget", source="linear", ticket_key="ACME-2", title="t")

    class _Boom(_FakeTracker):
        known_exceptions = (LinearAPIError,)

        async def set_status(self, key, status):
            raise LinearAPIError("boom")

    boom = _Boom()
    monkeypatch.setattr(Ship, "get_tracker", classmethod(lambda cls, source=None: boom))

    await item.set_ticket_status(TicketStatus.DONE)

    assert "aclose" in boom.calls  # closed even on failure


# --- Jira provider ----------------------------------------------------------


class _FakeJiraClient:
    base_url = "https://jira.test"

    def __init__(self) -> None:
        self.calls: list = []

    async def transition_issue(self, key, status_name):
        self.calls.append(("transition_issue", key, status_name))

    async def aclose(self):
        self.calls.append("aclose")


@pytest.mark.asyncio
async def test_jira_set_status_uses_transition():
    fake = _FakeJiraClient()
    provider = Jira(
        base_url="https://jira.test",
        email="a@b.com",
        api_token="tok",
        trigger_status="To Agent",
        client=object(),
    )
    provider._client = fake  # the unit seam is the API client, not HTTP
    await provider.set_status("PROJ-7", TicketStatus.DONE)
    await provider.set_status("PROJ-7", TicketStatus.TRIGGER)
    assert fake.calls == [
        ("transition_issue", "PROJ-7", "Done"),
        ("transition_issue", "PROJ-7", "To Agent"),
    ]


def test_jira_declares_known_exceptions():
    assert JiraAPIError in Jira.known_exceptions
    assert UnknownTicketError in Jira.known_exceptions
    assert httpx.HTTPError in Jira.known_exceptions


# --- JiraClient: transition lookups over the wire ---------------------------


def _jira_client(handler) -> JiraClient:
    wire = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return JiraClient(base_url="https://jira.test", email="a@b.com", api_token="tok", client=wire)


@pytest.mark.asyncio
async def test_jira_client_executes_the_transition_to_the_status():
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "transitions": [
                        {"id": "11", "to": {"name": "In Progress"}},
                        {"id": "21", "to": {"name": "Ready for Agent"}},
                    ]
                },
            )
        assert json.loads(request.content) == {"transition": {"id": "21"}}
        return httpx.Response(204)

    await _jira_client(handler).transition_issue("PROJ-7", "Ready for Agent")

    assert requests == [
        ("GET", "/rest/api/3/issue/PROJ-7/transitions"),
        ("POST", "/rest/api/3/issue/PROJ-7/transitions"),
    ]


@pytest.mark.asyncio
async def test_jira_client_translates_a_transitions_404_to_unknown_ticket():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"errorMessages": ["Issue does not exist"]})

    with pytest.raises(UnknownTicketError, match="PROJ-9 doesn't exist in Jira"):
        await _jira_client(handler).transition_issue("PROJ-9", "Ready for Agent")


@pytest.mark.asyncio
async def test_jira_client_keeps_other_failures_as_api_errors():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="bad gateway")

    with pytest.raises(JiraAPIError, match="-> 502"):
        await _jira_client(handler).transition_issue("PROJ-9", "Ready for Agent")


@pytest.mark.asyncio
async def test_jira_client_still_errors_without_a_matching_transition():
    # Already-at-trigger with no self-transition stays a JiraAPIError — druks
    # does not paper over Jira's transition model.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"transitions": [{"id": "31", "to": {"name": "Done"}}]})

    with pytest.raises(JiraAPIError, match="no transition to status"):
        await _jira_client(handler).transition_issue("PROJ-7", "Ready for Agent")


def test_jira_status_names_match_internal_tools_workflow():
    # The exact status names of an "Internal tools"-style Jira workflow
    # druks-managed tickets use. A regression here means set_status silently
    # fails against real Jira ("no transition to status X") — caught and logged,
    # so the ticket just never moves. Pin them.
    names = Jira._STATIC_STATUS_NAMES
    assert names[TicketStatus.IN_PROGRESS] == "In Progress"
    assert names[TicketStatus.IN_REVIEW] == "Waiting CR"
    assert names[TicketStatus.DONE] == "Done"
    # No cancel state in this workflow — abandoned work closes as Done.
    assert names[TicketStatus.CANCELED] == "Done"

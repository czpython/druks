import json
import logging

import httpx
import pytest
from druks.contrib.ship.extension import Ship
from druks.contrib.ship.ticketing.base import Tracker
from druks.contrib.ship.ticketing.enums import TicketStatus
from druks.contrib.ship.ticketing.exceptions import (
    JiraAPIError,
    LinearAPIError,
    TrackerStatusUnavailable,
    TrackerTicketNotFound,
)
from druks.contrib.ship.ticketing.jira import Jira, JiraClient
from druks.contrib.ship.ticketing.linear import Linear, LinearClient

from ship.factories import make_test_work_item, pin_ship_settings


def test_settings_require_provider_webhook_secrets_once_api_credentials_are_set():
    assert Ship.Settings(linear_api_key="x").clean() == {
        "linear_webhook_secret": "Required once the Linear API key is set."
    }
    assert Ship.Settings(jira_api_token="x").clean() == {
        "jira_webhook_secret": "Required once the Jira API token is set."
    }


def test_tracker_builds_providers_from_settings(monkeypatch):
    pin_ship_settings(
        monkeypatch,
        linear_api_key="lin_secret",
        linear_resting_status="Ready for Agent",
    )
    linear = Ship.tracker("linear")

    assert isinstance(linear, Linear)
    assert linear._status_names[TicketStatus.READY_FOR_AGENT] == "Ready for Agent"

    pin_ship_settings(
        monkeypatch,
        tracker="jira",
        jira_base_url="https://jira.test",
        jira_email="a@b.com",
        jira_api_token="jira_secret",
        jira_resting_status="Open",
    )
    jira = Ship.tracker("jira")

    assert isinstance(jira, Jira)
    assert jira._status_names[TicketStatus.READY_FOR_AGENT] == "Open"


def test_tracker_requires_chosen_source_and_complete_credentials(monkeypatch):
    pin_ship_settings(monkeypatch, linear_api_key="lin_secret")
    assert not Ship.tracker("github")
    assert not Ship.tracker("jira")

    pin_ship_settings(
        monkeypatch,
        tracker="jira",
        jira_base_url="https://jira.test",
        jira_email="a@b.com",
    )
    assert not Ship.tracker("jira")
    assert not Ship.tracker("linear")


def test_empty_resting_status_leaves_ready_for_agent_unmapped(monkeypatch):
    pin_ship_settings(monkeypatch, linear_api_key="lin_secret", linear_resting_status="")

    tracker = Ship.tracker("linear")

    assert TicketStatus.READY_FOR_AGENT not in tracker._status_names


def test_tracker_exception_contracts_and_provider_names():
    missing = TrackerTicketNotFound("Linear", "ENG-9999")
    unavailable = TrackerStatusUnavailable("Jira", "ENG-833", "Ready for Agent")

    assert (missing.tracker_name, missing.ticket_key, str(missing)) == (
        "Linear",
        "ENG-9999",
        "Linear knows no ENG-9999",
    )
    assert (
        unavailable.tracker_name,
        unavailable.ticket_key,
        unavailable.status_name,
        str(unavailable),
    ) == (
        "Jira",
        "ENG-833",
        "Ready for Agent",
        "Jira cannot move ENG-833 to status 'Ready for Agent'.",
    )
    assert (Linear.display_name, Jira.display_name) == ("Linear", "Jira")
    for provider, api_error in ((Linear, LinearAPIError), (Jira, JiraAPIError)):
        assert all(error in provider.known_exceptions for error in Tracker.known_exceptions)
        assert api_error in provider.known_exceptions
        assert httpx.HTTPError in provider.known_exceptions


def test_jira_api_error_retains_status_code():
    error = JiraAPIError("details", status_code=403)

    assert str(error) == "details"
    assert error.status_code == 403


def test_jira_status_names_match_internal_tools_workflow():
    names = Jira._STATIC_STATUS_NAMES

    assert names[TicketStatus.IN_PROGRESS] == "In Progress"
    assert names[TicketStatus.IN_REVIEW] == "Waiting CR"
    assert names[TicketStatus.DONE] == "Done"
    assert names[TicketStatus.CANCELED] == "Done"


class _FakeLinearClient:
    def __init__(self, outcomes: list[bool]) -> None:
        self.outcomes = outcomes
        self.calls: list[tuple] = []

    async def update_issue_status(self, ticket_key, status_name):
        self.calls.append(("update_issue_status", ticket_key, status_name))
        return self.outcomes.pop(0)

    async def aclose(self):
        self.calls.append(("aclose",))


class _FakeJiraClient:
    base_url = "https://jira.test"

    def __init__(self, outcomes: list[bool]) -> None:
        self.outcomes = outcomes
        self.calls: list[tuple] = []

    async def transition_issue(self, key, status_name):
        self.calls.append(("transition_issue", key, status_name))
        return self.outcomes.pop(0)

    async def aclose(self):
        self.calls.append(("aclose",))


@pytest.mark.parametrize(
    ("provider_class", "fake_class", "call_name", "constructor"),
    [
        (Linear, _FakeLinearClient, "update_issue_status", {"api_key": "lin_x"}),
        (
            Jira,
            _FakeJiraClient,
            "transition_issue",
            {"base_url": "https://jira.test", "email": "a@b.com", "api_token": "tok"},
        ),
    ],
)
async def test_providers_propagate_command_outcomes_and_discard_lifecycle_boolean(
    provider_class,
    fake_class,
    call_name,
    constructor,
):
    fake = fake_class([True, False, True])
    provider = provider_class(**constructor, ready_for_agent_status="Open", client=object())
    provider._client = fake

    assert await provider.move_ticket("ENG-833", "Trigger") is True
    assert await provider.move_ticket("ENG-833", "Trigger") is False
    assert await provider.set_status("ENG-833", TicketStatus.READY_FOR_AGENT) is None
    assert fake.calls == [
        (call_name, "ENG-833", "Trigger"),
        (call_name, "ENG-833", "Trigger"),
        (call_name, "ENG-833", "Open"),
    ]


async def test_lifecycle_unmapped_status_names_the_provider():
    provider = Linear(api_key="lin_x", client=object())
    provider._client = _FakeLinearClient([])

    with pytest.raises(ValueError, match="Linear has no configured status name"):
        await provider.set_status("ENG-833", TicketStatus.READY_FOR_AGENT)


def _linear_issue_payload(
    *,
    current: str = "Backlog",
    states: list[dict] | None = None,
    issue_id: str = "issue-uuid",
) -> dict:
    if states is None:
        states = [
            {"id": "backlog-id", "name": "Backlog"},
            {"id": "trigger-id", "name": "Ready for Agent"},
            {"id": "done-id", "name": "Done"},
        ]
    return {
        "data": {
            "issues": {
                "nodes": [
                    {
                        "id": issue_id,
                        "state": {"name": current},
                        "team": {"states": {"nodes": states}},
                    }
                ]
            }
        }
    }


def _linear_client(handler) -> LinearClient:
    transport_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return LinearClient(
        api_key="lin_x",
        tracker_name=Linear.display_name,
        client=transport_client,
    )


async def test_linear_changed_query_is_archived_inclusive_and_mutates_resolved_uuid():
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            return httpx.Response(200, json=_linear_issue_payload(), request=request)
        return httpx.Response(
            200,
            json={"data": {"issueUpdate": {"success": True}}},
            request=request,
        )

    client = _linear_client(handler)
    try:
        changed = await client.update_issue_status("ENG-833", "Ready for Agent")
    finally:
        await client.aclose()

    assert changed is True
    query = requests[0]["query"]
    assert "$issueNumber: Float!" in query
    assert "issues(" in query
    assert "first: 1" in query
    assert "includeArchived: true" in query
    assert "identifier" not in query
    assert requests[0]["variables"] == {"teamKey": "ENG", "issueNumber": 833.0}
    assert requests[1]["variables"] == {"issueId": "issue-uuid", "statusId": "trigger-id"}
    assert "issue {" not in requests[1]["query"]


async def test_linear_same_state_returns_false_without_target_lookup_or_mutation():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        payload = _linear_issue_payload(current="Ready for Agent")
        payload["data"]["issues"]["nodes"][0]["team"] = None
        return httpx.Response(200, json=payload, request=request)

    client = _linear_client(handler)
    try:
        changed = await client.update_issue_status("ENG-833", "Ready for Agent")
    finally:
        await client.aclose()

    assert changed is False
    assert calls == 1


@pytest.mark.parametrize("ticket_key", ["ENG", "-1", "ENG-0", "ENG-nope"])
async def test_linear_rejects_malformed_lifecycle_keys_before_http(ticket_key):
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500, request=request)

    client = _linear_client(handler)
    try:
        with pytest.raises(LinearAPIError, match="Invalid Linear ticket key"):
            await client.update_issue_status(ticket_key, "Done")
    finally:
        await client.aclose()

    assert calls == 0


async def test_linear_empty_collection_is_typed_not_found():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"data": {"issues": {"nodes": []}}},
            request=request,
        )

    client = _linear_client(handler)
    try:
        with pytest.raises(TrackerTicketNotFound, match="Linear knows no ENG-9999"):
            await client.update_issue_status("ENG-9999", "Done")
    finally:
        await client.aclose()


async def test_linear_unavailable_status_carries_the_caller_key():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_linear_issue_payload(states=[]), request=request)

    client = _linear_client(handler)
    try:
        with pytest.raises(TrackerStatusUnavailable) as raised:
            await client.update_issue_status("ENG-833", "Missing")
    finally:
        await client.aclose()

    assert raised.value.ticket_key == "ENG-833"
    assert raised.value.status_name == "Missing"


@pytest.mark.parametrize(
    "body",
    [
        {"data": {"issues": None}},
        {"data": {"issues": {"nodes": None}}},
        {"data": {"issues": {"nodes": [None]}}},
        {"data": {"issues": {"nodes": [{"id": None, "state": {"name": "Backlog"}}]}}},
        {
            "data": {
                "issues": {
                    "nodes": [
                        {
                            "id": "uuid",
                            "state": {"name": "Backlog"},
                            "team": {"states": {"nodes": [None]}},
                        }
                    ]
                }
            }
        },
    ],
)
async def test_linear_malformed_query_payloads_are_provider_errors(body):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body, request=request)

    client = _linear_client(handler)
    try:
        with pytest.raises(LinearAPIError):
            await client.update_issue_status("ENG-833", "Ready for Agent")
    finally:
        await client.aclose()


@pytest.mark.parametrize("update", [None, {}, {"success": False}, {"success": "yes"}])
async def test_linear_requires_true_mutation_success(update):
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        body = _linear_issue_payload() if calls == 1 else {"data": {"issueUpdate": update}}
        return httpx.Response(200, json=body, request=request)

    client = _linear_client(handler)
    try:
        with pytest.raises(LinearAPIError, match="did not succeed"):
            await client.update_issue_status("ENG-833", "Ready for Agent")
    finally:
        await client.aclose()


@pytest.mark.parametrize("status_code", [200, 400])
async def test_linear_structured_errors_precede_http_status_and_keep_exact_list(status_code):
    errors = [{"message": "denied", "extensions": {"code": "FORBIDDEN"}}]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json={"errors": errors}, request=request)

    client = _linear_client(handler)
    try:
        with pytest.raises(LinearAPIError) as raised:
            await client.update_issue_status("ENG-833", "Done")
    finally:
        await client.aclose()

    assert str(errors) in str(raised.value)
    assert not hasattr(raised.value, "errors")


async def test_linear_unusable_non_success_body_keeps_httpx_status_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="not-json", request=request)

    client = _linear_client(handler)
    try:
        with pytest.raises(httpx.HTTPStatusError):
            await client.update_issue_status("ENG-833", "Done")
    finally:
        await client.aclose()


async def test_linear_default_fifty_team_states_can_find_the_target():
    states = [{"id": f"state-{number}", "name": f"State {number}"} for number in range(49)]
    states.append({"id": "done-id", "name": "Done"})
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        body = (
            _linear_issue_payload(states=states)
            if calls == 1
            else {"data": {"issueUpdate": {"success": True}}}
        )
        return httpx.Response(200, json=body, request=request)

    client = _linear_client(handler)
    try:
        assert await client.update_issue_status("ENG-833", "Done") is True
    finally:
        await client.aclose()


async def test_linear_done_lifecycle_uses_archived_collection_and_mutates():
    documents = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        documents.append(body)
        response = (
            _linear_issue_payload()
            if len(documents) == 1
            else {"data": {"issueUpdate": {"success": True}}}
        )
        return httpx.Response(200, json=response, request=request)

    transport_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = Linear(api_key="lin_x", client=transport_client)
    try:
        await provider.set_status("ENG-833", TicketStatus.DONE)
    finally:
        await provider.aclose()

    assert "includeArchived: true" in documents[0]["query"]
    assert documents[1]["variables"]["statusId"] == "done-id"


def _jira_client(handler) -> JiraClient:
    transport_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return JiraClient(
        base_url="https://jira.test",
        email="a@b.com",
        api_token="token",
        tracker_name=Jira.display_name,
        client=transport_client,
    )


async def test_jira_changed_reads_discovers_and_posts_once():
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, str(request.url), request.content))
        if len(requests) == 1:
            return httpx.Response(
                200,
                json={"fields": {"status": {"name": "Backlog"}}},
                request=request,
            )
        if len(requests) == 2:
            return httpx.Response(
                200,
                json={"transitions": [{"id": "31", "to": {"name": "Ready for Agent"}}]},
                request=request,
            )
        return httpx.Response(204, request=request)

    client = _jira_client(handler)
    try:
        assert await client.transition_issue("ENG-833", "Ready for Agent") is True
    finally:
        await client.aclose()

    assert [(method, url) for method, url, _ in requests] == [
        ("GET", "https://jira.test/rest/api/3/issue/ENG-833?fields=status"),
        ("GET", "https://jira.test/rest/api/3/issue/ENG-833/transitions"),
        ("POST", "https://jira.test/rest/api/3/issue/ENG-833/transitions"),
    ]
    assert json.loads(requests[2][2]) == {"transition": {"id": "31"}}


async def test_jira_same_state_is_read_only():
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={"fields": {"status": {"name": "Ready for Agent"}}},
            request=request,
        )

    client = _jira_client(handler)
    try:
        assert await client.transition_issue("ENG-833", "Ready for Agent") is False
    finally:
        await client.aclose()

    assert len(requests) == 1


@pytest.mark.parametrize("status_code", [401, 500])
async def test_jira_non_404_initial_read_stays_jira_error(status_code):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, text="provider detail", request=request)

    client = _jira_client(handler)
    try:
        with pytest.raises(JiraAPIError) as raised:
            await client.transition_issue("ENG-833", "Done")
    finally:
        await client.aclose()

    assert raised.value.status_code == status_code
    assert "provider detail" in str(raised.value)


async def test_jira_initial_read_404_is_typed_not_found_and_stops():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(404, text="hidden", request=request)

    client = _jira_client(handler)
    try:
        with pytest.raises(TrackerTicketNotFound, match="Jira knows no ENG-833"):
            await client.transition_issue("ENG-833", "Done")
    finally:
        await client.aclose()

    assert calls == 1


async def test_jira_discovery_404_remains_jira_error():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(
                200,
                json={"fields": {"status": {"name": "Backlog"}}},
                request=request,
            )
        return httpx.Response(404, text="workflow hidden", request=request)

    client = _jira_client(handler)
    try:
        with pytest.raises(JiraAPIError) as raised:
            await client.transition_issue("ENG-833", "Done")
    finally:
        await client.aclose()

    assert raised.value.status_code == 404


async def test_jira_missing_target_is_typed_unavailable():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        body = {"fields": {"status": {"name": "Backlog"}}} if calls == 1 else {"transitions": []}
        return httpx.Response(200, json=body, request=request)

    client = _jira_client(handler)
    try:
        with pytest.raises(TrackerStatusUnavailable) as raised:
            await client.transition_issue("ENG-833", "Done")
    finally:
        await client.aclose()

    assert raised.value.ticket_key == "ENG-833"


@pytest.mark.parametrize(
    "response_kwargs",
    [
        {"text": "not-json"},
        {"json": []},
        {"json": {}},
        {"json": {"fields": {"status": {"name": None}}}},
    ],
)
async def test_jira_malformed_successful_initial_payload_is_provider_error(response_kwargs):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, request=request, **response_kwargs)

    client = _jira_client(handler)
    try:
        with pytest.raises(JiraAPIError):
            await client.transition_issue("ENG-833", "Done")
    finally:
        await client.aclose()


async def test_jira_done_lifecycle_uses_the_same_read_discover_post_primitive():
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.method)
        if len(requests) == 1:
            return httpx.Response(
                200,
                json={"fields": {"status": {"name": "In Progress"}}},
                request=request,
            )
        if len(requests) == 2:
            return httpx.Response(
                200,
                json={"transitions": [{"id": "done", "to": {"name": "Done"}}]},
                request=request,
            )
        return httpx.Response(204, request=request)

    transport_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = Jira(
        base_url="https://jira.test",
        email="a@b.com",
        api_token="token",
        client=transport_client,
    )
    try:
        await provider.set_status("ENG-833", TicketStatus.DONE)
    finally:
        await provider.aclose()

    assert requests == ["GET", "GET", "POST"]


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


async def test_ticket_state_pushes_status_and_closes(druks_db, monkeypatch):
    item = make_test_work_item(repo="acme/widget", source="linear", ticket_key="ACME-1", title="t")
    fake = _FakeTracker()
    monkeypatch.setattr(Ship, "tracker", classmethod(lambda cls, source: fake))

    await item.set_ticket_status(TicketStatus.DONE)

    assert fake.calls == [("ACME-1", TicketStatus.DONE), "aclose"]


async def test_ticket_state_skips_non_tracker_source(druks_db):
    item = make_test_work_item(repo="acme/widget", source="github", ticket_key="#5", title="t")

    await item.set_ticket_status(TicketStatus.DONE)


async def test_jira_lifecycle_initial_404_is_logged_swallowed_and_stops(
    druks_db,
    monkeypatch,
    caplog,
):
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(404, text="hidden issue detail", request=request)

    transport_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = Jira(
        base_url="https://jira.test",
        email="a@b.com",
        api_token="token",
        client=transport_client,
    )
    monkeypatch.setattr(Ship, "tracker", classmethod(lambda cls, source: provider))
    item = make_test_work_item(repo="acme/widget", source="jira", ticket_key="ENG-833", title="t")

    with caplog.at_level(logging.WARNING):
        await item.set_ticket_status(TicketStatus.DONE)

    assert len(requests) == 1
    assert transport_client.is_closed
    assert "Could not sync jira ticket ENG-833 to done" in caplog.text

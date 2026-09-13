import json

import httpx
import pytest
from conftest import connect_service
from druks.apps.settings import field_choice_source, field_choices, field_visibility
from druks.contrib.software_factory.app import (
    SoftwareFactory,
    check_tracker_identity,
    list_tracker_status_choices,
)
from druks.contrib.software_factory.ticketing.druks import DruksTracker
from druks.contrib.software_factory.ticketing.enums import TicketStatus
from druks.contrib.software_factory.ticketing.jira import Jira
from druks.contrib.software_factory.ticketing.linear import Linear
from druks.core import services
from druks.core.apis.exceptions import JiraAPIError, LinearAPIError, UnknownTicketError
from druks.core.apis.jira import JiraClient
from druks.core.apis.linear import LinearClient
from druks.services import ServiceConnectError

from software_factory.factories import make_test_work_item


def _pin_software_factory_settings(monkeypatch, **values):
    settings = SoftwareFactory.Settings(**values)

    async def _settings(cls):
        return settings

    monkeypatch.setattr(SoftwareFactory, "settings", classmethod(_settings))


async def _connect_linear():
    return await connect_service(
        "linear",
        identity={"actor": "druks", "workspace": "Acme"},
        secrets={"api_key": "lin_secret", "webhook_secret": "lin-hook"},
    )


async def _connect_jira():
    return await connect_service(
        "jira",
        identity={"base_url": "https://jira.test", "email": "a@b.com", "display_name": "druks"},
        secrets={"api_token": "jira_secret", "webhook_secret": "jira-hook"},
    )


async def test_tracker_builds_linear_from_the_service_row(monkeypatch):
    await _connect_linear()
    _pin_software_factory_settings(monkeypatch, trigger_status="To Agent")

    tracker = await SoftwareFactory.get_tracker("linear")

    assert isinstance(tracker, Linear)
    assert tracker._client.api_key == "lin_secret"
    assert tracker._status_names == {
        TicketStatus.TRIGGER: "To Agent",
        TicketStatus.IN_PROGRESS: "In Progress",
        TicketStatus.IN_REVIEW: "In Review",
        TicketStatus.DONE: "Done",
        TicketStatus.BACKLOG: "Backlog",
    }


async def test_tracker_builds_jira_from_the_service_row(monkeypatch):
    await _connect_jira()
    _pin_software_factory_settings(monkeypatch, tracker="jira", trigger_status="To Agent")

    tracker = await SoftwareFactory.get_tracker("jira")

    assert isinstance(tracker, Jira)
    assert tracker._client.base_url == "https://jira.test"
    assert tracker._status_names == {
        TicketStatus.TRIGGER: "To Agent",
        TicketStatus.IN_PROGRESS: "In Progress",
        TicketStatus.IN_REVIEW: "In Review",
        TicketStatus.DONE: "Done",
        TicketStatus.BACKLOG: "Backlog",
    }


async def test_tracker_is_none_for_github_and_a_disconnected_identity(monkeypatch):
    await _connect_linear()
    _pin_software_factory_settings(monkeypatch)

    assert not await SoftwareFactory.get_tracker("github")
    assert not await SoftwareFactory.get_tracker("jira")

    _pin_software_factory_settings(monkeypatch, tracker="jira")
    assert not await SoftwareFactory.get_tracker("jira")
    assert not await SoftwareFactory.get_tracker("linear")


async def test_linear_lists_each_status_name_once_with_its_type(monkeypatch):
    await _connect_linear()
    _pin_software_factory_settings(monkeypatch)

    async def list_workflow_states(self):
        return [
            {"name": "In Review", "type": "started"},
            {"name": "Done", "type": "completed"},
            {"name": "In Review", "type": "started"},
        ]

    monkeypatch.setattr(LinearClient, "list_workflow_states", list_workflow_states)

    assert await list_tracker_status_choices() == [
        ("In Review", "In Review (started)"),
        ("Done", "Done (completed)"),
    ]


async def test_jira_lists_each_status_name_once_with_its_category(monkeypatch):
    await _connect_jira()
    _pin_software_factory_settings(monkeypatch, tracker="jira")

    async def list_statuses(self):
        return [
            {"name": "Waiting CR", "statusCategory": {"name": "In Progress"}},
            {"name": "Done", "statusCategory": {"name": "Done"}},
            {"name": "Done", "statusCategory": {"name": "Done"}},
        ]

    monkeypatch.setattr(JiraClient, "list_statuses", list_statuses)

    assert await list_tracker_status_choices() == [
        ("Waiting CR", "Waiting CR (in progress)"),
        ("Done", "Done (done)"),
    ]


async def test_status_choices_are_empty_without_a_connected_tracker_that_answers(monkeypatch):
    await _connect_linear()
    _pin_software_factory_settings(monkeypatch)

    async def list_workflow_states(self):
        raise LinearAPIError("Linear is down")

    monkeypatch.setattr(LinearClient, "list_workflow_states", list_workflow_states)

    assert await list_tracker_status_choices() == []
    _pin_software_factory_settings(monkeypatch, tracker="jira")
    assert await list_tracker_status_choices() == []
    _pin_software_factory_settings(monkeypatch, tracker="druks")
    assert await list_tracker_status_choices() == []


async def test_empty_resting_status_leaves_backlog_unmapped(monkeypatch):
    await _connect_linear()
    _pin_software_factory_settings(monkeypatch, resting_status="")

    tracker = await SoftwareFactory.get_tracker("linear")

    assert TicketStatus.BACKLOG not in tracker._status_names


async def test_linear_verify_stores_the_actor_and_workspace(monkeypatch):
    async def fake_post(self, url, **kwargs):
        return httpx.Response(
            200,
            json={"data": {"viewer": {"name": "druks"}, "organization": {"name": "Acme"}}},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    facts = await services.Linear.verify(services.Linear.Settings(api_key="k", webhook_secret="s"))

    assert facts == {"actor": "druks", "workspace": "Acme"}


async def test_linear_verify_rejects_a_bad_key_without_echoing(monkeypatch):
    async def fake_post(self, url, **kwargs):
        return httpx.Response(
            400,
            json={"errors": [{"message": "auth boom-marker"}]},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    with pytest.raises(ServiceConnectError) as raised:
        await services.Linear.verify(services.Linear.Settings(api_key="bad", webhook_secret="s"))

    assert "boom-marker" not in str(raised.value)
    assert "bad" not in str(raised.value)


async def test_jira_verify_stores_the_display_name(monkeypatch):
    seen = []

    async def fake_get(self, url, **kwargs):
        seen.append(url)
        return httpx.Response(
            200, json={"displayName": "Druks Bot"}, request=httpx.Request("GET", url)
        )

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
        return httpx.Response(401, text="nope", request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

    with pytest.raises(ServiceConnectError, match="did not accept"):
        await services.Jira.verify(
            services.Jira.Settings(
                base_url="https://jira.test", email="a@b.com", api_token="bad", webhook_secret="s"
            )
        )


async def test_tracker_check_accepts_trackerless_by_choice(monkeypatch):
    _pin_software_factory_settings(monkeypatch, tracker="none")

    result = await check_tracker_identity()

    assert result.ok
    assert "choice" in result.detail


async def test_tracker_check_reports_a_selected_connected_tracker(monkeypatch):
    await _connect_linear()
    _pin_software_factory_settings(monkeypatch)

    result = await check_tracker_identity()

    assert result.ok
    assert "linear" in result.detail


async def test_tracker_check_pends_a_selected_unconnected_tracker(monkeypatch):
    _pin_software_factory_settings(monkeypatch, tracker="jira")

    result = await check_tracker_identity()

    assert not result.ok
    assert result.pending
    assert "jira" in result.detail


async def test_tracker_check_accepts_the_board_without_a_service(monkeypatch):
    _pin_software_factory_settings(monkeypatch, tracker="druks")

    result = await check_tracker_identity()

    assert result.ok
    assert result.detail == "this appliance"
    assert not result.pending


def test_status_settings_show_for_linear_and_jira_with_the_selected_tracker_statuses():
    fields = SoftwareFactory.Settings.model_fields
    assert field_choices(fields["tracker"]) == ["none", "linear", "jira", "druks"]
    for status in ("trigger", "in_progress", "in_review", "done", "resting"):
        assert field_visibility(fields[f"{status}_status"]) == ("tracker", ["linear", "jira"])
        assert field_choice_source(fields[f"{status}_status"]) is list_tracker_status_choices


async def test_tracker_builds_the_board_without_credentials(monkeypatch):
    _pin_software_factory_settings(monkeypatch, tracker="druks")

    tracker = await SoftwareFactory.get_tracker("druks")

    assert isinstance(tracker, DruksTracker)
    assert not await SoftwareFactory.get_tracker("linear")


class _FakeLinearClient:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def update_issue_status(self, issue_id, status_name):
        self.calls.append(("update_issue_status", issue_id, status_name))

    async def aclose(self):
        self.calls.append(("aclose",))


async def test_set_status_maps_the_ticket_status_to_a_provider_name():
    fake = _FakeLinearClient()
    provider = Linear(
        api_key="lin_x",
        status_names={
            TicketStatus.DONE: "Done",
            TicketStatus.BACKLOG: "Backlog",
            TicketStatus.TRIGGER: "To Agent",
        },
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


async def test_set_status_unmapped_raises():
    provider = Linear(api_key="lin_x", status_names={TicketStatus.BACKLOG: ""}, client=object())
    provider._client = _FakeLinearClient()
    with pytest.raises(ValueError, match="no configured status"):
        await provider.set_status("ACME-270", TicketStatus.BACKLOG)
    with pytest.raises(ValueError, match="no configured status"):
        await provider.set_status("ACME-270", TicketStatus.TRIGGER)


def test_linear_declares_known_exceptions():
    assert LinearAPIError in Linear.known_exceptions
    assert UnknownTicketError in Linear.known_exceptions
    assert httpx.HTTPError in Linear.known_exceptions


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
                        "states": {
                            "nodes": [{"id": state_id, "name": name} for state_id, name in states]
                        }
                    },
                }
            }
        },
    )


async def test_linear_client_skips_the_mutation_when_already_at_the_status():
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return _linear_issue_response(
            current="Ready for Agent", states=[("current-id", "Ready for Agent")]
        )

    result = await _linear_client(handler).update_issue_status("ENG-831", "Ready for Agent")

    assert result == {"identifier": "ENG-831", "status": "Ready for Agent", "changed": False}
    assert len(requests) == 1
    assert "mutation" not in requests[0]["query"]


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


async def test_linear_client_lists_workflow_states_across_pages():
    pages = iter(
        [
            {
                "nodes": [{"name": "Todo", "type": "unstarted"}],
                "pageInfo": {"hasNextPage": True, "endCursor": "cursor-1"},
            },
            {
                "nodes": [{"name": "Done", "type": "completed"}],
                "pageInfo": {"hasNextPage": False, "endCursor": None},
            },
        ]
    )
    cursors = []

    def handler(request: httpx.Request) -> httpx.Response:
        cursors.append(json.loads(request.content)["variables"]["after"])
        return httpx.Response(200, json={"data": {"workflowStates": next(pages)}})

    states = await _linear_client(handler).list_workflow_states()

    assert [state["name"] for state in states] == ["Todo", "Done"]
    assert cursors == [None, "cursor-1"]


async def test_linear_client_translates_a_null_issue_to_unknown_ticket():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"issue": None}})

    with pytest.raises(UnknownTicketError, match="ENG-9999 doesn't exist in Linear"):
        await _linear_client(handler).update_issue_status("ENG-9999", "Ready for Agent")


def _tracker_stub(fake):
    async def get_tracker(cls, source=None):
        return fake

    return classmethod(get_tracker)


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


async def test_ticket_state_pushes_status(monkeypatch):
    item = await make_test_work_item(
        repo="acme/widget", source="linear", ticket_key="ACME-1", title="t"
    )
    fake = _FakeTracker()
    monkeypatch.setattr(SoftwareFactory, "get_tracker", _tracker_stub(fake))

    await item.set_ticket_status(TicketStatus.DONE)

    assert fake.calls == [("ACME-1", TicketStatus.DONE), "aclose"]


async def test_ticket_state_skips_non_tracker_source():
    item = await make_test_work_item(
        repo="acme/widget", source="github", ticket_key="#5", title="t"
    )
    await item.set_ticket_status(TicketStatus.DONE)


async def test_ticket_state_closes_on_failure(monkeypatch):
    item = await make_test_work_item(
        repo="acme/widget", source="linear", ticket_key="ACME-2", title="t"
    )

    class _Boom(_FakeTracker):
        known_exceptions = (LinearAPIError,)

        async def set_status(self, key, status):
            raise LinearAPIError("boom")

    boom = _Boom()
    monkeypatch.setattr(SoftwareFactory, "get_tracker", _tracker_stub(boom))

    await item.set_ticket_status(TicketStatus.DONE)

    assert "aclose" in boom.calls


class _FakeJiraClient:
    base_url = "https://jira.test"

    def __init__(self) -> None:
        self.calls: list = []

    async def transition_issue(self, key, status_name):
        self.calls.append(("transition_issue", key, status_name))

    async def aclose(self):
        self.calls.append("aclose")


async def test_jira_set_status_uses_transition():
    fake = _FakeJiraClient()
    provider = Jira(
        base_url="https://jira.test",
        email="a@b.com",
        api_token="tok",
        status_names={TicketStatus.DONE: "Done", TicketStatus.TRIGGER: "To Agent"},
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


def _jira_client(handler) -> JiraClient:
    wire = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return JiraClient(base_url="https://jira.test", email="a@b.com", api_token="tok", client=wire)


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


async def test_jira_client_translates_a_transitions_404_to_unknown_ticket():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"errorMessages": ["Issue does not exist"]})

    with pytest.raises(UnknownTicketError, match="PROJ-9 doesn't exist in Jira"):
        await _jira_client(handler).transition_issue("PROJ-9", "Ready for Agent")


async def test_jira_client_keeps_other_failures_as_api_errors():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="bad gateway")

    with pytest.raises(JiraAPIError, match="-> 502"):
        await _jira_client(handler).transition_issue("PROJ-9", "Ready for Agent")


async def test_jira_client_still_errors_without_a_matching_transition():
    # A ticket already at the status has no transition to it, and Druks does not hide that.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"transitions": [{"id": "31", "to": {"name": "Done"}}]})

    with pytest.raises(JiraAPIError, match="no transition to status"):
        await _jira_client(handler).transition_issue("PROJ-7", "Ready for Agent")


async def test_jira_client_sends_the_resolution_a_transition_requires():
    posted = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            assert request.url.params["expand"] == "transitions.fields"
            resolution = {
                "required": True,
                "allowedValues": [{"id": "2", "name": "Won't Do"}, {"id": "1", "name": "Done"}],
            }
            return httpx.Response(
                200,
                json={
                    "transitions": [
                        {"id": "31", "to": {"name": "Done"}, "fields": {"resolution": resolution}}
                    ]
                },
            )
        posted.append(json.loads(request.content))
        return httpx.Response(204)

    await _jira_client(handler).transition_issue("PROJ-7", "Done")

    assert posted == [{"transition": {"id": "31"}, "fields": {"resolution": {"id": "1"}}}]


async def test_jira_client_lists_the_statuses_of_active_workflows():
    paths = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(200, json=[{"name": "To Do"}, {"name": "Done"}])

    statuses = await _jira_client(handler).list_statuses()

    assert [status["name"] for status in statuses] == ["To Do", "Done"]
    # The status search endpoint needs Jira admin rights. This one needs Browse projects.
    assert paths == ["/rest/api/3/status"]

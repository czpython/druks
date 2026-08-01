import pytest
from druks.contrib.ship.extension import Ship
from druks.contrib.ship.ticketing.enums import TicketStatus
from druks.contrib.ship.ticketing.exceptions import JiraAPIError, LinearAPIError
from druks.contrib.ship.ticketing.jira import Jira
from druks.contrib.ship.ticketing.linear import Linear


def _pin_ship_settings(monkeypatch, **values):
    settings = Ship.Settings(**values)
    monkeypatch.setattr(Ship, "settings", classmethod(lambda cls: settings))


def test_settings_require_linear_webhook_secret_once_the_api_key_is_set():
    settings = Ship.Settings(linear_api_key="x")

    assert settings.clean() == {"linear_webhook_secret": "Required once the Linear API key is set."}


def test_settings_require_jira_webhook_secret_once_the_api_token_is_set():
    settings = Ship.Settings(jira_api_token="x")

    assert settings.clean() == {"jira_webhook_secret": "Required once the Jira API token is set."}


# --- Ship.tracker: source → configured tracker ------------------------------


def test_tracker_builds_linear_from_settings(monkeypatch):
    _pin_ship_settings(
        monkeypatch, linear_api_key="lin_secret", linear_resting_status="Ready for Agent"
    )

    tracker = Ship.tracker("linear")

    assert isinstance(tracker, Linear)
    assert tracker._status_names[TicketStatus.READY_FOR_AGENT] == "Ready for Agent"


def test_tracker_builds_jira_from_settings(monkeypatch):
    _pin_ship_settings(
        monkeypatch,
        jira_base_url="https://jira.test",
        jira_email="a@b.com",
        jira_api_token="jira_secret",
        jira_resting_status="Open",
    )

    tracker = Ship.tracker("jira")

    assert isinstance(tracker, Jira)
    assert tracker._status_names[TicketStatus.READY_FOR_AGENT] == "Open"


def test_tracker_is_none_for_github_and_missing_credentials(monkeypatch):
    _pin_ship_settings(monkeypatch, linear_api_key="lin_secret")

    assert not Ship.tracker("github")
    assert not Ship.tracker("jira")

    _pin_ship_settings(monkeypatch, jira_base_url="https://jira.test", jira_email="a@b.com")
    assert not Ship.tracker("jira")
    assert not Ship.tracker("linear")


def test_empty_resting_status_leaves_ready_for_agent_unmapped(monkeypatch):
    _pin_ship_settings(monkeypatch, linear_api_key="lin_secret", linear_resting_status="")

    tracker = Ship.tracker("linear")

    assert TicketStatus.READY_FOR_AGENT not in tracker._status_names


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
    provider = Linear(api_key="lin_x", ready_for_agent_status="Ready for Agent", client=object())
    provider._client = fake  # the unit seam is the API client, not HTTP
    await provider.set_status("ACME-270", TicketStatus.DONE)
    await provider.set_status("ACME-270", TicketStatus.READY_FOR_AGENT)
    assert fake.calls == [
        ("update_issue_status", "ACME-270", "Done"),
        ("update_issue_status", "ACME-270", "Ready for Agent"),
    ]


@pytest.mark.asyncio
async def test_set_status_unmapped_raises():
    provider = Linear(api_key="lin_x", client=object())
    provider._client = _FakeLinearClient()
    with pytest.raises(ValueError, match="no configured status"):
        await provider.set_status("ACME-270", TicketStatus.READY_FOR_AGENT)


def test_linear_declares_known_exceptions():
    import httpx

    assert LinearAPIError in Linear.known_exceptions
    assert httpx.HTTPError in Linear.known_exceptions


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
    from ship.factories import make_test_work_item

    item = make_test_work_item(repo="acme/widget", source="linear", ticket_key="ACME-1", title="t")
    fake = _FakeTracker()
    monkeypatch.setattr(Ship, "tracker", classmethod(lambda cls, source: fake))

    await item.set_ticket_status(TicketStatus.DONE)

    assert fake.calls == [("ACME-1", TicketStatus.DONE), "aclose"]


@pytest.mark.asyncio
async def test_ticket_state_skips_non_tracker_source(druks_db):
    from ship.factories import make_test_work_item

    item = make_test_work_item(repo="acme/widget", source="github", ticket_key="#5", title="t")
    # github has no tracker — a no-op that must not raise.
    await item.set_ticket_status(TicketStatus.DONE)


@pytest.mark.asyncio
async def test_ticket_state_closes_on_failure(druks_db, monkeypatch):
    from ship.factories import make_test_work_item

    item = make_test_work_item(repo="acme/widget", source="linear", ticket_key="ACME-2", title="t")

    class _Boom(_FakeTracker):
        known_exceptions = (LinearAPIError,)

        async def set_status(self, key, status):
            raise LinearAPIError("boom")

    boom = _Boom()
    monkeypatch.setattr(Ship, "tracker", classmethod(lambda cls, source: boom))

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
    provider = Jira(base_url="https://jira.test", email="a@b.com", api_token="tok", client=object())
    provider._client = fake  # the unit seam is the API client, not HTTP
    await provider.set_status("PROJ-7", TicketStatus.DONE)
    assert fake.calls == [("transition_issue", "PROJ-7", "Done")]


def test_jira_declares_known_exceptions():
    import httpx

    assert JiraAPIError in Jira.known_exceptions
    assert httpx.HTTPError in Jira.known_exceptions


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

import pytest
from druks.ticketing.enums import TicketStatus
from druks.ticketing.exceptions import TrackerNotConfigured
from druks.ticketing.helpers import get_tracker
from druks.ticketing.jira import Jira
from druks.ticketing.linear import Linear


class _FakeLinearClient:
    """Records the client calls the provider delegates to — no HTTP."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def update_issue_status(self, issue_id, status_name):
        self.calls.append(("update_issue_status", issue_id, status_name))

    async def aclose(self):
        self.calls.append(("aclose",))


def _linear_with(fake: _FakeLinearClient, *, status_names=None) -> Linear:
    """A Linear provider wired to the fake client (skips real-client init)."""
    provider = Linear.__new__(Linear)
    provider._client = fake  # type: ignore[attr-defined]
    provider._status_names = status_names or {  # type: ignore[attr-defined]
        TicketStatus.IN_PROGRESS: "In Progress",
        TicketStatus.DONE: "Done",
        TicketStatus.CANCELED: "Canceled",
        TicketStatus.READY_FOR_AGENT: "Ready for Agent",
    }
    return provider


@pytest.mark.asyncio
async def test_set_status_maps_the_ticket_status_to_a_provider_name():
    fake = _FakeLinearClient()
    provider = _linear_with(fake)
    await provider.set_status("ACME-270", TicketStatus.DONE)
    await provider.set_status("ACME-270", TicketStatus.READY_FOR_AGENT)
    assert fake.calls == [
        ("update_issue_status", "ACME-270", "Done"),
        ("update_issue_status", "ACME-270", "Ready for Agent"),
    ]


@pytest.mark.asyncio
async def test_set_status_unmapped_raises():
    provider = _linear_with(_FakeLinearClient(), status_names={TicketStatus.DONE: "Done"})
    with pytest.raises(ValueError, match="no configured status"):
        await provider.set_status("ACME-270", TicketStatus.IN_REVIEW)


def test_get_tracker_resolves_configured_linear(tmp_path, monkeypatch):
    from druks.testing import make_settings
    from druks.ticketing import linear

    monkeypatch.setattr(
        linear,
        "load_settings",
        lambda: make_settings(tmp_path, linear_api_key="lin_abc"),
    )
    tracker = get_tracker("linear")
    assert isinstance(tracker, Linear)
    assert tracker.source == "linear"


def test_get_tracker_unknown_source_raises():
    with pytest.raises(KeyError):
        get_tracker("github")


def test_get_tracker_unconfigured_raises(tmp_path, monkeypatch):
    from druks.testing import make_settings
    from druks.ticketing import linear

    # linear_api_key defaults to None — provider exists but isn't configured.
    monkeypatch.setattr(linear, "load_settings", lambda: make_settings(tmp_path))
    with pytest.raises(TrackerNotConfigured):
        get_tracker("linear")


def test_linear_declares_known_exceptions():
    import httpx
    from druks.core.apis.linear import LinearAPIError

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
    from druks.contrib.ship import models

    from ship.factories import make_test_work_item

    item = make_test_work_item(repo="acme/widget", source="linear", ticket_key="ACME-1", title="t")
    fake = _FakeTracker()
    monkeypatch.setattr(models, "get_tracker", lambda source, **_: fake)

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
    from druks.contrib.ship import models
    from druks.core.apis.linear import LinearAPIError

    from ship.factories import make_test_work_item

    item = make_test_work_item(repo="acme/widget", source="linear", ticket_key="ACME-2", title="t")

    class _Boom(_FakeTracker):
        known_exceptions = (LinearAPIError,)

        async def set_status(self, key, status):
            raise LinearAPIError("boom")

    boom = _Boom()
    monkeypatch.setattr(models, "get_tracker", lambda source, **_: boom)

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


def _jira_with(fake: _FakeJiraClient) -> Jira:
    provider = Jira.__new__(Jira)
    provider._client = fake  # type: ignore[attr-defined]
    provider._status_names = {  # type: ignore[attr-defined]
        TicketStatus.IN_PROGRESS: "In Progress",
        TicketStatus.DONE: "Done",
        TicketStatus.READY_FOR_AGENT: "Ready for Agent",
    }
    return provider


@pytest.mark.asyncio
async def test_jira_set_status_uses_transition():
    fake = _FakeJiraClient()
    await _jira_with(fake).set_status("PROJ-7", TicketStatus.DONE)
    assert fake.calls == [("transition_issue", "PROJ-7", "Done")]


def test_jira_declares_known_exceptions():
    import httpx
    from druks.core.apis.jira import JiraAPIError

    assert JiraAPIError in Jira.known_exceptions
    assert httpx.HTTPError in Jira.known_exceptions


def test_get_tracker_resolves_configured_jira(tmp_path, monkeypatch):
    from druks.testing import make_settings
    from druks.ticketing import jira

    monkeypatch.setattr(
        jira,
        "load_settings",
        lambda: make_settings(
            tmp_path,
            jira_base_url="https://jira.test",
            jira_email="a@b.com",
            jira_api_token="tok",
        ),
    )
    tracker = get_tracker("jira", ready_for_agent_status="Open")
    assert isinstance(tracker, Jira)
    assert tracker.source == "jira"
    # The operator names their own READY_FOR_AGENT status; the move looks it up here.
    assert tracker._status_names[TicketStatus.READY_FOR_AGENT] == "Open"


def test_jira_status_names_match_internal_tools_workflow():
    # The exact status names of an "Internal tools"-style Jira workflow
    # druks-managed tickets use. A regression here means set_status silently
    # fails against real Jira ("no transition to status X") — caught and logged,
    # so the ticket just never moves. Pin them.
    from druks.ticketing.jira import _STATIC_STATUS_NAMES

    assert _STATIC_STATUS_NAMES[TicketStatus.IN_PROGRESS] == "In Progress"
    assert _STATIC_STATUS_NAMES[TicketStatus.IN_REVIEW] == "Waiting CR"
    assert _STATIC_STATUS_NAMES[TicketStatus.DONE] == "Done"
    # No cancel state in this workflow — abandoned work closes as Done.
    assert _STATIC_STATUS_NAMES[TicketStatus.CANCELED] == "Done"


def test_get_tracker_unconfigured_jira_raises(tmp_path, monkeypatch):
    from druks.testing import make_settings
    from druks.ticketing import jira

    monkeypatch.setattr(jira, "load_settings", lambda: make_settings(tmp_path))
    with pytest.raises(TrackerNotConfigured):
        get_tracker("jira")

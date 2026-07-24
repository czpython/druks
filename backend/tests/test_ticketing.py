import pytest
from druks.ticketing.datastructures import Ticket
from druks.ticketing.enums import SemanticStatus, StatusKind
from druks.ticketing.exceptions import TrackerNotConfigured
from druks.ticketing.helpers import get_tracker
from druks.ticketing.jira import Jira
from druks.ticketing.linear import Linear

# A representative LinearClient.get_issue() payload — the shape Linear._normalize
# must map onto a Ticket.
SAMPLE_ISSUE = {
    "id": "uuid-issue-1",
    "identifier": "ACME-270",
    "title": "Add local verification baseline",
    "description": "the problem",
    "url": "https://linear.app/x/issue/ACME-270",
    "priority": 2,
    "updatedAt": "2026-06-05T00:00:00Z",
    "state": {"id": "s1", "name": "In Progress", "type": "started"},
    "project": {"id": "p1", "name": "acme-mcp"},
    "team": {"id": "team-1", "name": "Engineering"},
    "labels": {"nodes": [{"name": "bug"}, {"name": "druks-scoped"}]},
    "assignee": {"id": "a1", "email": "dev@clawhaven.com"},
    "comments": {
        "nodes": [
            {"body": "first", "createdAt": "2026-06-01", "user": {"email": "u@x.com", "name": "U"}},
        ],
    },
}


class _FakeLinearClient:
    """Records the client calls the provider delegates to — no HTTP."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def get_issue(self, key):
        self.calls.append(("get_issue", key))
        return SAMPLE_ISSUE

    async def update_issue_status(self, issue_id, status_name):
        self.calls.append(("update_issue_status", issue_id, status_name))

    async def add_issue_comment(self, issue_id, body):
        self.calls.append(("add_issue_comment", issue_id, body))
        return "comment-1"

    async def add_issue_label(self, *, issue_id, team_id, label_name):
        self.calls.append(("add_issue_label", issue_id, team_id, label_name))

    async def update_issue_description(self, issue_id, description):
        self.calls.append(("update_issue_description", issue_id, description))

    async def aclose(self):
        self.calls.append(("aclose",))


def _linear_with(fake: _FakeLinearClient, *, status_names=None) -> Linear:
    """A Linear provider wired to the fake client (skips real-client init)."""
    provider = Linear.__new__(Linear)
    provider._client = fake  # type: ignore[attr-defined]
    provider._status_names = status_names or {  # type: ignore[attr-defined]
        SemanticStatus.IN_PROGRESS: "In Progress",
        SemanticStatus.DONE: "Done",
        SemanticStatus.CANCELED: "Canceled",
        SemanticStatus.READY_FOR_AGENT: "Ready for Agent",
    }
    return provider


def test_normalize_maps_linear_dict_to_ticket():
    ticket = Linear.__new__(Linear)._normalize(SAMPLE_ISSUE)
    assert isinstance(ticket, Ticket)
    assert ticket.provider == "linear"
    assert ticket.id == "uuid-issue-1"
    assert ticket.key == "ACME-270"
    assert ticket.title == "Add local verification baseline"
    assert ticket.description == "the problem"
    assert ticket.status_name == "In Progress"
    assert ticket.status_kind is StatusKind.STARTED
    assert ticket.project_name == "acme-mcp"
    assert ticket.container_id == "team-1"
    assert ticket.labels == ["bug", "druks-scoped"]
    assert ticket.has_label("druks-scoped")
    assert not ticket.has_label("nope")
    assert ticket.raw is SAMPLE_ISSUE


def test_normalize_tolerates_missing_optional_blocks():
    ticket = Linear.__new__(Linear)._normalize(
        {"id": "i", "identifier": "ACME-1", "title": "t"},
    )
    assert ticket.project_name is None
    assert ticket.container_id is None
    assert ticket.labels == []
    assert ticket.status_kind is StatusKind.UNKNOWN


@pytest.mark.asyncio
async def test_fetch_ticket_normalizes():
    fake = _FakeLinearClient()
    ticket = await _linear_with(fake).fetch_ticket("ACME-270")
    assert fake.calls == [("get_issue", "ACME-270")]
    assert ticket.key == "ACME-270"


@pytest.mark.asyncio
async def test_set_status_maps_semantic_to_provider_name():
    fake = _FakeLinearClient()
    provider = _linear_with(fake)
    ticket = provider._normalize(SAMPLE_ISSUE)
    await provider.set_status(ticket, SemanticStatus.DONE)
    await provider.set_status(ticket, SemanticStatus.READY_FOR_AGENT)
    assert fake.calls == [
        ("update_issue_status", "uuid-issue-1", "Done"),
        ("update_issue_status", "uuid-issue-1", "Ready for Agent"),
    ]


@pytest.mark.asyncio
async def test_set_status_unmapped_raises():
    provider = _linear_with(_FakeLinearClient(), status_names={SemanticStatus.DONE: "Done"})
    ticket = provider._normalize(SAMPLE_ISSUE)
    with pytest.raises(ValueError, match="no configured status"):
        await provider.set_status(ticket, SemanticStatus.IN_REVIEW)


def test_get_tracker_resolves_configured_linear(tmp_path, monkeypatch):
    from conftest import make_settings
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
    from conftest import make_settings
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


# --- WorkItem.set_remote_status: the status-push consumer -------------------


class _FakeTracker:
    known_exceptions: tuple = ()

    def __init__(self) -> None:
        self.calls: list = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.aclose()

    async def set_status(self, ticket, status):
        self.calls.append((ticket.provider, ticket.key, status))

    async def aclose(self):
        self.calls.append("aclose")


@pytest.mark.asyncio
async def test_remote_state_pushes_status(db_session, monkeypatch):
    from conftest import make_test_work_item
    from druks.build import models

    item = make_test_work_item(repo="acme/widget", source="linear", remote_key="ACME-1", title="t")
    fake = _FakeTracker()
    monkeypatch.setattr(models, "get_tracker", lambda source, **_: fake)

    await item.set_remote_status(SemanticStatus.DONE)

    assert fake.calls == [("linear", "ACME-1", SemanticStatus.DONE), "aclose"]


@pytest.mark.asyncio
async def test_remote_state_skips_non_tracker_source(db_session):
    from conftest import make_test_work_item

    item = make_test_work_item(repo="acme/widget", source="github", remote_key="#5", title="t")
    # github has no tracker — a no-op that must not raise.
    await item.set_remote_status(SemanticStatus.DONE)


@pytest.mark.asyncio
async def test_remote_state_closes_on_failure(db_session, monkeypatch):
    from conftest import make_test_work_item
    from druks.build import models
    from druks.core.apis.linear import LinearAPIError

    item = make_test_work_item(repo="acme/widget", source="linear", remote_key="ACME-2", title="t")

    class _Boom(_FakeTracker):
        known_exceptions = (LinearAPIError,)

        async def set_status(self, ticket, status):
            raise LinearAPIError("boom")

    boom = _Boom()
    monkeypatch.setattr(models, "get_tracker", lambda source, **_: boom)

    await item.set_remote_status(SemanticStatus.DONE)

    assert "aclose" in boom.calls  # closed even on failure


# --- Jira provider (Phase B, step 1: engine ops, no rich ADF) ----------------

SAMPLE_JIRA_ISSUE = {
    "id": "10042",
    "key": "PROJ-7",
    "fields": {
        "summary": "Add the widget",
        "description": {
            "type": "doc",
            "version": 1,
            "content": [
                {"type": "paragraph", "content": [{"type": "text", "text": "the problem"}]}
            ],
        },
        "status": {"name": "In Progress", "statusCategory": {"key": "indeterminate"}},
        "labels": ["bug", "druks-scoped"],
        "priority": {"id": "2", "name": "High"},
        "project": {"id": "10000", "key": "PROJ", "name": "acme-app"},
        "assignee": {"accountId": "acc-1", "emailAddress": "dev@clawhaven.com"},
        "comment": {
            "comments": [
                {
                    "id": "c1",
                    "body": {
                        "type": "doc",
                        "version": 1,
                        "content": [
                            {"type": "paragraph", "content": [{"type": "text", "text": "first"}]},
                        ],
                    },
                    "author": {"accountId": "u1", "emailAddress": "u@x.com"},
                    "created": "2026-06-01T00:00:00.000+0000",
                },
            ],
        },
    },
}


class _FakeJiraClient:
    base_url = "https://jira.test"

    def __init__(self) -> None:
        self.calls: list = []

    async def get_issue(self, key):
        self.calls.append(("get_issue", key))
        return SAMPLE_JIRA_ISSUE

    async def transition_issue(self, key, status_name):
        self.calls.append(("transition_issue", key, status_name))

    async def add_comment(self, key, body_adf):
        self.calls.append(("add_comment", key, body_adf))
        return "jira-comment-1"

    async def add_label(self, key, label):
        self.calls.append(("add_label", key, label))

    async def set_description(self, key, description_adf):
        self.calls.append(("set_description", key, description_adf))

    async def aclose(self):
        self.calls.append("aclose")


def _jira_with(fake: _FakeJiraClient) -> Jira:
    provider = Jira.__new__(Jira)
    provider._client = fake  # type: ignore[attr-defined]
    provider._status_names = {  # type: ignore[attr-defined]
        SemanticStatus.IN_PROGRESS: "In Progress",
        SemanticStatus.DONE: "Done",
        SemanticStatus.READY_FOR_AGENT: "Ready for Agent",
    }
    return provider


def test_jira_normalize_maps_issue_to_ticket():
    ticket = _jira_with(_FakeJiraClient())._normalize(SAMPLE_JIRA_ISSUE)
    assert ticket.provider == "jira"
    assert ticket.id == "10042"
    assert ticket.key == "PROJ-7"
    assert ticket.title == "Add the widget"
    # The agent self-fetches the description/comments; _normalize keeps only the
    # structured engine fields and the raw payload (write splice reads raw ADF).
    assert ticket.description is None
    assert ticket.url == "https://jira.test/browse/PROJ-7"
    assert ticket.status_name == "In Progress"
    assert ticket.status_kind is StatusKind.STARTED
    assert ticket.labels == ["bug", "druks-scoped"]
    assert ticket.has_label("druks-scoped")
    assert ticket.assignee_email == "dev@clawhaven.com"
    assert ticket.assignee_id == "acc-1"
    assert ticket.project_name == "acme-app"
    assert ticket.project_id == "10000"
    assert ticket.container_id == "PROJ"  # project key drives sub-task/label


def test_jira_normalize_tolerates_empty_fields():
    ticket = _jira_with(_FakeJiraClient())._normalize({"id": "1", "key": "P-1", "fields": {}})
    assert ticket.description is None
    assert ticket.labels == []
    assert ticket.status_kind is StatusKind.UNKNOWN


@pytest.mark.asyncio
async def test_jira_set_status_uses_transition():
    fake = _FakeJiraClient()
    provider = _jira_with(fake)
    ticket = provider._normalize(SAMPLE_JIRA_ISSUE)
    await provider.set_status(ticket, SemanticStatus.DONE)
    assert ("transition_issue", "PROJ-7", "Done") in fake.calls


@pytest.mark.asyncio
async def test_jira_fetch_ticket_normalizes():
    fake = _FakeJiraClient()
    ticket = await _jira_with(fake).fetch_ticket("PROJ-7")
    assert fake.calls == [("get_issue", "PROJ-7")]
    assert ticket.key == "PROJ-7"


def test_jira_declares_known_exceptions():
    import httpx
    from druks.core.apis.jira import JiraAPIError

    assert JiraAPIError in Jira.known_exceptions
    assert httpx.HTTPError in Jira.known_exceptions


def test_get_tracker_resolves_configured_jira(tmp_path, monkeypatch):
    from conftest import make_settings
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
    # The operator's READY_FOR_AGENT status name the caller supplies, mapped onto
    # the semantic status for the actual move.
    assert tracker._status_names[SemanticStatus.READY_FOR_AGENT] == "Open"


def test_jira_status_names_match_internal_tools_workflow():
    # The exact status names of an "Internal tools"-style Jira workflow
    # druks-managed tickets use. A regression here means set_status silently
    # fails against real Jira ("no transition to status X") — caught and logged,
    # so the ticket just never moves. Pin them.
    from druks.ticketing.jira import _STATIC_STATUS_NAMES

    assert _STATIC_STATUS_NAMES[SemanticStatus.IN_PROGRESS] == "In Progress"
    assert _STATIC_STATUS_NAMES[SemanticStatus.IN_REVIEW] == "Waiting CR"
    assert _STATIC_STATUS_NAMES[SemanticStatus.DONE] == "Done"
    # No cancel state in this workflow — abandoned work closes as Done.
    assert _STATIC_STATUS_NAMES[SemanticStatus.CANCELED] == "Done"


def test_get_tracker_unconfigured_jira_raises(tmp_path, monkeypatch):
    from conftest import make_settings
    from druks.ticketing import jira

    monkeypatch.setattr(jira, "load_settings", lambda: make_settings(tmp_path))
    with pytest.raises(TrackerNotConfigured):
        get_tracker("jira")

from types import SimpleNamespace
from typing import Any, cast

from druks.core.webhooks import github as github_webhooks
from druks.core.webhooks.github import GitHubEvents
from druks.testing import make_settings


def _events(tmp_path, *, payload, event="issues"):
    events = GitHubEvents(
        request=cast(Any, SimpleNamespace(headers={"x-github-event": event})),
        kwargs={},
        settings=make_settings(tmp_path),
    )
    events._data_cached = payload
    return events


def _labeled(*, number=7, label="ready-for-agent", labels=None, assignee=None):
    return {
        "action": "labeled",
        "label": {"name": label},
        "issue": {
            "number": number,
            "title": "Contacts import drops the last row",
            "html_url": f"https://github.com/acme/widget/issues/{number}",
            "labels": [{"name": name} for name in (labels or [label])],
            "assignee": assignee,
        },
        "repository": {"full_name": "acme/widget", "name": "widget"},
    }


def _capture(monkeypatch):
    published = []

    async def _emit(event_type, **kwargs):
        published.append((event_type, kwargs["payload"]))

    monkeypatch.setattr(github_webhooks, "publish", _emit)
    return published


def test_a_labelled_issue_dispatches_to_the_issues_handler(tmp_path):
    # get_action composes event + action, so `issues` + `labeled` has to land on
    # on_issues_labeled for any of this to run.
    assert _events(tmp_path, payload=_labeled()).get_action() == "issues_labeled"


async def test_labelling_publishes_a_ticket_transition(tmp_path, monkeypatch):
    published = _capture(monkeypatch)

    await _events(tmp_path, payload=_labeled()).on_issues_labeled()

    assert len(published) == 1
    event_type, payload = published[0]
    assert event_type == "ticket.transitioned"
    assert payload["source"] == "github"
    assert payload["status"] == "ready-for-agent"
    assert payload["title"] == "Contacts import drops the last row"
    assert payload["url"] == "https://github.com/acme/widget/issues/7"


async def test_the_identifier_carries_the_repository(tmp_path, monkeypatch):
    # Issue numbers are only unique within a repo, so the key has to say which.
    published = _capture(monkeypatch)

    await _events(tmp_path, payload=_labeled(number=42)).on_issues_labeled()

    assert published[0][1]["identifier"] == "acme/widget#42"


async def test_the_bare_repo_name_routes_to_a_project_repo(tmp_path, monkeypatch):
    # ProjectRepo.lookup matches a bare name, the way it does a Linear project.
    published = _capture(monkeypatch)

    await _events(tmp_path, payload=_labeled()).on_issues_labeled()

    assert published[0][1]["project_name"] == "widget"


async def test_every_label_on_the_issue_travels_for_routing(tmp_path, monkeypatch):
    published = _capture(monkeypatch)

    payload = _labeled(labels=["ready-for-agent", "widget", "bug"])
    await _events(tmp_path, payload=payload).on_issues_labeled()

    assert published[0][1]["labels"] == ["ready-for-agent", "widget", "bug"]


async def test_an_unassigned_issue_publishes_no_assignee(tmp_path, monkeypatch):
    published = _capture(monkeypatch)

    await _events(tmp_path, payload=_labeled(assignee=None)).on_issues_labeled()

    assert published[0][1]["assignee_name"] is None
    assert published[0][1]["assignee_email"] is None


async def test_an_assigned_issue_publishes_the_login_but_never_an_email(tmp_path, monkeypatch):
    # GitHub webhooks carry no address; inventing one would misattribute a run.
    published = _capture(monkeypatch)

    payload = _labeled(assignee={"login": "octocat"})
    await _events(tmp_path, payload=payload).on_issues_labeled()

    assert published[0][1]["assignee_name"] == "octocat"
    assert published[0][1]["assignee_email"] is None


async def test_an_assignee_never_selects_an_account(tmp_path, monkeypatch):
    # assignee_id drives the account lookup. A GitHub login is not an identity
    # any grant issuer vouches for, so the build must not resolve one from it.
    published = _capture(monkeypatch)

    payload = _labeled(assignee={"login": "octocat", "id": 583231})
    await _events(tmp_path, payload=payload).on_issues_labeled()

    assert published[0][1]["assignee_id"] is None

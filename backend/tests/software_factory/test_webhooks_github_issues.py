from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import druks.contrib.software_factory.subscribers as subs
from conftest import connect_service
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


def _labeled(
    *,
    number=7,
    label="ready-for-agent",
    assignee=None,
    sender_type="User",
    sender_login="octocat",
):
    return {
        "action": "labeled",
        "label": {"name": label},
        "issue": {
            "number": number,
            "title": "Contacts import drops the last row",
            "html_url": f"https://github.com/acme/widget/issues/{number}",
            "labels": [{"name": label}],
            "assignee": assignee,
        },
        "repository": {"full_name": "acme/widget", "name": "widget"},
        "sender": {"type": sender_type, "login": sender_login},
    }


def _capture(monkeypatch, module=github_webhooks):
    published = []

    async def _emit(event_type, **kwargs):
        published.append((event_type, kwargs))

    monkeypatch.setattr(module, "publish", _emit)
    return published


async def _connect_github():
    await connect_service(
        "github",
        identity={"app_id": "1", "slug": "druks-operator"},
        secrets={"private_key": "pem", "webhook_secret": "hook-secret"},
    )


def test_a_labelled_issue_dispatches_to_the_issues_handler(tmp_path):
    # get_action composes event + action, so `issues` + `labeled` has to land on
    # on_issues_labeled for any of this to run.
    assert _events(tmp_path, payload=_labeled()).get_action() == "issues_labeled"


async def test_a_persons_label_publishes_a_neutral_fact(tmp_path, monkeypatch):
    published = _capture(monkeypatch)

    payload = _labeled(assignee={"login": "hubot"})
    await _events(tmp_path, payload=payload).on_issues_labeled()

    # Core knows nothing about work items: the fact is the label, not a transition.
    assert published == [
        (
            "issue.labeled",
            {
                "repo": "acme/widget",
                "number": 7,
                "payload": {
                    "label": "ready-for-agent",
                    "title": "Contacts import drops the last row",
                    "url": "https://github.com/acme/widget/issues/7",
                    "assignee": "hubot",
                },
            },
        )
    ]


async def test_another_apps_label_publishes_nothing(druks_db, tmp_path, monkeypatch):
    await _connect_github()
    published = _capture(monkeypatch)

    payload = _labeled(sender_type="Bot", sender_login="renovate[bot]")
    await _events(tmp_path, payload=payload).on_issues_labeled()

    assert published == []


async def test_the_apps_own_label_publishes(druks_db, tmp_path, monkeypatch):
    # The start route asks for a build by setting the trigger label through the
    # App, so the App's own label has to reach the funnel.
    await _connect_github()
    published = _capture(monkeypatch)

    payload = _labeled(sender_type="Bot", sender_login="druks-operator[bot]")
    await _events(tmp_path, payload=payload).on_issues_labeled()

    assert [event for event, _ in published] == ["issue.labeled"]


async def test_a_label_becomes_a_ticket_transition(monkeypatch):
    published = _capture(monkeypatch, module=subs)

    await subs.issue_label_transitions_the_ticket(
        repo="acme/widget",
        number=42,
        payload={
            "label": "ready-for-agent",
            "title": "Contacts import drops the last row",
            "url": "https://github.com/acme/widget/issues/42",
            "assignee": "octocat",
        },
    )

    assert published == [
        (
            "ticket.transitioned",
            {
                "payload": {
                    "source": "github",
                    # Issue numbers are only unique within a repo, so the key says which.
                    "identifier": "acme/widget#42",
                    "status": "ready-for-agent",
                    "title": "Contacts import drops the last row",
                    "url": "https://github.com/acme/widget/issues/42",
                    # The full name matches one repo exactly. A label such as
                    # ``api`` would match any owner's ``api``, so none routes it.
                    "project_name": "acme/widget",
                    "labels": [],
                    # A login is not an address and selects no account.
                    "assignee_id": None,
                    "assignee_email": None,
                    "assignee_name": "octocat",
                },
            },
        )
    ]


async def test_the_trigger_label_dispatches_a_github_build(monkeypatch):
    settings = subs.SoftwareFactory.Settings(tracker="github", trigger_status="ready-for-agent")

    async def _settings(cls):
        return settings

    monkeypatch.setattr(subs.SoftwareFactory, "settings", classmethod(_settings))
    build = AsyncMock()
    monkeypatch.setattr(subs.Build, "dispatch", build)

    await subs.issue_label_transitions_the_ticket(
        repo="acme/widget",
        number=7,
        payload={
            "label": "ready-for-agent",
            "title": "Contacts import drops the last row",
            "url": "https://github.com/acme/widget/issues/7",
            "assignee": None,
        },
    )

    build.assert_awaited_once()
    assert build.await_args.kwargs["ticket"]["identifier"] == "acme/widget#7"

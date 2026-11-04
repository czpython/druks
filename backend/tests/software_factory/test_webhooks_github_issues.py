from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import druks.contrib.software_factory.subscribers as subs
from conftest import connect_service
from druks.core.webhooks import github as github_webhooks
from druks.core.webhooks.github import GitHubEvents
from druks.testing import make_settings


def _events(tmp_path, *, payload):
    events = GitHubEvents(
        request=cast(Any, SimpleNamespace(headers={"x-github-event": "issues"})),
        kwargs={},
        settings=make_settings(tmp_path),
    )
    events._data_cached = payload
    return events


def _labeled(*, assignee=None, sender_type="User", sender_login="octocat"):
    return {
        "action": "labeled",
        "label": {"name": "ready-for-agent"},
        "issue": {
            "id": 4242,
            "number": 7,
            "title": "Contacts import drops the last row",
            "html_url": "https://github.com/acme/widget/issues/7",
            "assignee": assignee,
        },
        "repository": {"full_name": "acme/widget"},
        "sender": {"type": sender_type, "login": sender_login, "id": 583231},
    }


def _capture(monkeypatch):
    published = []

    async def _emit(event_type, **kwargs):
        published.append((event_type, kwargs))

    monkeypatch.setattr(github_webhooks, "publish", _emit)
    return published


async def _connect_github():
    await connect_service(
        "github",
        identity={"app_id": "1", "slug": "druks-operator"},
        secrets={"private_key": "pem", "webhook_secret": "hook-secret"},
    )


async def test_a_persons_label_publishes_the_label_and_who_put_it(druks_db, tmp_path, monkeypatch):
    await _connect_github()
    published = _capture(monkeypatch)

    payload = _labeled(assignee={"login": "hubot", "id": 480938})
    await _events(tmp_path, payload=payload).on_issues_labeled()

    assert published == [
        (
            "issue.labeled",
            {
                "repo": "acme/widget",
                "number": 7,
                "payload": {
                    "issue_id": 4242,
                    "label": "ready-for-agent",
                    "actor_id": 583231,
                    "title": "Contacts import drops the last row",
                    "url": "https://github.com/acme/widget/issues/7",
                    "assignee": "hubot",
                    "assignee_id": 480938,
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
    # The start route asks for a build by setting the trigger label through the App.
    await _connect_github()
    published = _capture(monkeypatch)

    payload = _labeled(sender_type="Bot", sender_login="druks-operator[bot]")
    await _events(tmp_path, payload=payload).on_issues_labeled()

    assert [event for event, _ in published] == ["issue.labeled"]


async def test_the_trigger_label_dispatches_a_build_for_the_issue(monkeypatch):
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
            "issue_id": 4242,
            "label": "ready-for-agent",
            "actor_id": 583231,
            "title": "Contacts import drops the last row",
            "url": "https://github.com/acme/widget/issues/7",
            "assignee": None,
            "assignee_id": None,
        },
    )

    build.assert_awaited_once_with(
        ticket={
            "source": "github",
            "ticket_id": "4242",
            "identifier": "acme/widget#7",
            "status": "ready-for-agent",
            "title": "Contacts import drops the last row",
            "url": "https://github.com/acme/widget/issues/7",
            "project_name": "acme/widget",
            "labels": [],
            "actor_id": "583231",
            "assignee_id": None,
            "assignee_email": None,
            "assignee_name": None,
        }
    )

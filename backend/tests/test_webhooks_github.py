from types import SimpleNamespace
from typing import Any, cast

import pytest
from druks.core.webhooks import github as github_webhooks
from druks.core.webhooks.github import GitHubEvents
from druks.testing import make_settings


def _events(tmp_path, *, payload):
    events = GitHubEvents(
        request=cast(Any, SimpleNamespace(headers={"x-github-event": "pull_request"})),
        kwargs={},
        settings=make_settings(tmp_path),
    )
    events._data_cached = payload
    return events


def _pull_request(*, action):
    return {
        "action": action,
        "repository": {"full_name": "acme/widget"},
        "pull_request": {
            "number": 12,
            "title": "Import contacts",
            "body": "Closes #7",
            "draft": False,
            "user": {"login": "octocat"},
            "author_association": "MEMBER",
            "head": {
                "ref": "agent/eng-1",
                "sha": "0f3a2c1d",
                "repo": {"full_name": "acme/widget"},
            },
            "base": {"ref": "main"},
        },
    }


def _capture(monkeypatch):
    published = []

    async def _emit(event_type, **kwargs):
        published.append((event_type, kwargs))

    monkeypatch.setattr(github_webhooks, "publish", _emit)
    return published


def _issue(*, action, body="It drops the labels."):
    return {
        "action": action,
        "repository": {"full_name": "acme/widget"},
        "issue": {
            "number": 7,
            "title": "The webhook drops labels",
            "body": body,
            "html_url": "https://github.com/acme/widget/issues/7",
            "user": {"login": "passerby"},
            "author_association": "NONE",
        },
    }


@pytest.mark.parametrize(
    ("action", "handler", "fact"),
    [
        ("opened", "on_issues_opened", "issue.opened"),
        ("edited", "on_issues_edited", "issue.edited"),
        ("reopened", "on_issues_reopened", "issue.reopened"),
    ],
)
async def test_an_issue_action_publishes_its_fact(tmp_path, monkeypatch, action, handler, fact):
    published = _capture(monkeypatch)

    await getattr(_events(tmp_path, payload=_issue(action=action)), handler)()

    assert published == [
        (
            fact,
            {
                "repo": "acme/widget",
                "number": 7,
                "payload": {
                    "title": "The webhook drops labels",
                    "body": "It drops the labels.",
                    "url": "https://github.com/acme/widget/issues/7",
                    "author": "passerby",
                    "author_can_write": False,
                },
            },
        )
    ]


async def test_an_issue_without_a_body_publishes_an_empty_one(tmp_path, monkeypatch):
    published = _capture(monkeypatch)

    await _events(tmp_path, payload=_issue(action="opened", body=None)).on_issues_opened()

    assert published[0][1]["payload"]["body"] == ""


@pytest.mark.parametrize(
    ("action", "handler", "fact"),
    [
        ("opened", "on_pull_request_opened", "pr.opened"),
        ("synchronize", "on_pull_request_synchronize", "pr.synchronized"),
        ("edited", "on_pull_request_edited", "pr.edited"),
        ("ready_for_review", "on_pull_request_ready_for_review", "pr.ready_for_review"),
    ],
)
async def test_a_pull_request_action_publishes_its_fact(
    tmp_path, monkeypatch, action, handler, fact
):
    published = _capture(monkeypatch)

    await getattr(_events(tmp_path, payload=_pull_request(action=action)), handler)()

    assert published == [
        (
            fact,
            {
                "repo": "acme/widget",
                "pr_number": 12,
                "payload": {
                    "branch": "agent/eng-1",
                    "is_fork": False,
                    "base_branch": "main",
                    "head_sha": "0f3a2c1d",
                    "title": "Import contacts",
                    "body": "Closes #7",
                    "draft": False,
                    "author": "octocat",
                    "author_can_write": True,
                },
            },
        )
    ]


@pytest.mark.parametrize("head_repo", [{"full_name": "stranger/widget"}, None])
async def test_a_pull_request_from_a_fork_says_so(tmp_path, monkeypatch, head_repo):
    published = _capture(monkeypatch)
    delivery = _pull_request(action="opened")
    delivery["pull_request"]["head"]["repo"] = head_repo

    await _events(tmp_path, payload=delivery).on_pull_request_opened()

    assert published[0][1]["payload"]["is_fork"] is True

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
            "head": {"ref": "agent/eng-1", "sha": "0f3a2c1d"},
            "base": {"ref": "main"},
        },
    }


def _capture(monkeypatch):
    published = []

    async def _emit(event_type, **kwargs):
        published.append((event_type, kwargs))

    monkeypatch.setattr(github_webhooks, "publish", _emit)
    return published


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

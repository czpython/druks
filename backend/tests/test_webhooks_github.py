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
            "author_association": "OWNER",
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


def _github(monkeypatch, **answers):
    asked = []

    async def can_write(repo, username):
        asked.append(username)
        return answers.get(username, False)

    async def get_client(owner_or_repo):
        return SimpleNamespace(can_write=can_write)

    monkeypatch.setattr(github_webhooks.Github, "get_client", get_client)
    return asked


def _issue(*, action, is_pull_request=False, sender_type="User", association="NONE"):
    issue = {
        "id": 4242,
        "number": 7,
        "title": "The webhook drops labels",
        "html_url": "https://github.com/acme/widget/issues/7",
        "labels": [{"id": 1, "name": "bug"}],
        "user": {"login": "reporter"},
    }
    if is_pull_request:
        issue["pull_request"] = {}
    return {
        "action": action,
        "repository": {"full_name": "acme/widget"},
        "issue": issue,
        "sender": {"type": sender_type, "login": "passerby", "id": 99},
        "comment": {"id": 501, "body": "Still broken", "author_association": association},
    }


TICKET = {
    "source": "github",
    "ticket_id": "4242",
    "identifier": "acme/widget#7",
    "title": "The webhook drops labels",
    "url": "https://github.com/acme/widget/issues/7",
    "project_name": "acme/widget",
    "labels": ["bug"],
    # Whoever acted, not the issue's author.
    "actor_id": "99",
}

COMMENT = {
    "comment_id": 501,
    "body": "Still broken",
    "author": "passerby",
    "author_is_bot": False,
    "author_can_write": False,
}


@pytest.mark.parametrize(
    ("action", "handler", "fact"),
    [
        ("opened", "on_issues_opened", "ticket.created"),
        ("edited", "on_issues_edited", "ticket.updated"),
        ("reopened", "on_issues_reopened", "ticket.updated"),
    ],
)
async def test_an_issue_action_publishes_a_tracker_fact(
    tmp_path, monkeypatch, action, handler, fact
):
    published = _capture(monkeypatch)

    await getattr(_events(tmp_path, payload=_issue(action=action)), handler)()

    assert published == [(fact, {"payload": TICKET})]


async def test_an_issue_comment_publishes_ticket_commented(tmp_path, monkeypatch):
    published = _capture(monkeypatch)
    _github(monkeypatch)

    await _events(tmp_path, payload=_issue(action="created")).on_issue_comment_created()

    assert published == [("ticket.commented", {"payload": TICKET | COMMENT})]


async def test_a_pull_request_comment_publishes_pr_commented(tmp_path, monkeypatch):
    published = _capture(monkeypatch)
    _github(monkeypatch)
    payload = _issue(action="created", is_pull_request=True)

    await _events(tmp_path, payload=payload).on_issue_comment_created()

    assert published == [
        (
            "pr.commented",
            {
                "repo": "acme/widget",
                "pr_number": 7,
                "payload": COMMENT | {"actor_id": "99", "review_thread_id": None},
            },
        )
    ]


async def test_an_apps_comment_says_its_author_is_a_bot(tmp_path, monkeypatch):
    published = _capture(monkeypatch)
    payload = _issue(action="created", sender_type="Bot")

    await _events(tmp_path, payload=payload).on_issue_comment_created()

    assert published[0][1]["payload"]["author_is_bot"] is True


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


@pytest.mark.parametrize(
    ("commit_id", "is_latest_commit"), [("0f3a2c1d", True), ("9e8d7c6b", False)]
)
async def test_a_review_says_whether_it_is_of_the_latest_commit(
    tmp_path, monkeypatch, commit_id, is_latest_commit
):
    published = _capture(monkeypatch)
    payload = {
        **_pull_request(action="submitted"),
        "sender": {"login": "alice", "type": "User"},
        "review": {
            "state": "approved",
            "author_association": "OWNER",
            "body": None,
            "commit_id": commit_id,
        },
    }

    await _events(tmp_path, payload=payload).on_pull_request_review_submitted()

    [(event_type, published_event)] = published
    assert event_type == "pr.review_submitted"
    assert published_event["payload"]["is_latest_commit"] is is_latest_commit


@pytest.mark.parametrize("association", ["MEMBER", "COLLABORATOR", "CONTRIBUTOR"])
@pytest.mark.parametrize("can_write", [True, False])
async def test_a_review_from_anyone_but_the_owner_asks_github_whether_they_write(
    tmp_path, monkeypatch, association, can_write
):
    published = _capture(monkeypatch)
    _github(monkeypatch, alice=can_write)

    payload = {
        **_pull_request(action="submitted"),
        "sender": {"login": "alice", "type": "User"},
        "review": {
            "state": "approved",
            "author_association": association,
            "body": None,
            "commit_id": "0f3a2c1d",
        },
    }

    await _events(tmp_path, payload=payload).on_pull_request_review_submitted()

    assert published[0][1]["payload"]["author_can_write"] is can_write


@pytest.mark.parametrize(("sender_type", "association"), [("User", "OWNER"), ("Bot", "NONE")])
async def test_the_owner_or_a_bot_is_not_looked_up(tmp_path, monkeypatch, sender_type, association):
    _capture(monkeypatch)
    asked = _github(monkeypatch)
    payload = _issue(action="created", sender_type=sender_type, association=association)

    await _events(tmp_path, payload=payload).on_issue_comment_created()

    assert asked == []


async def test_a_completed_check_suite_names_its_commit_and_branch(tmp_path, monkeypatch):
    published = _capture(monkeypatch)
    payload = {
        "action": "completed",
        "repository": {"full_name": "acme/widget"},
        "check_suite": {"head_sha": "0f3a2c1d", "head_branch": "agent/eng-1"},
    }

    await _events(tmp_path, payload=payload).on_check_suite_completed()

    [(event_type, published_event)] = published
    assert event_type == "commit.check_suite_completed"
    assert published_event == {
        "repo": "acme/widget",
        "payload": {"head_sha": "0f3a2c1d", "branches": ["agent/eng-1"]},
    }


@pytest.mark.parametrize(("state", "is_pending"), [("failure", False), ("pending", True)])
async def test_a_status_names_its_state_and_the_branches_its_commit_heads(
    tmp_path, monkeypatch, state, is_pending
):
    published = _capture(monkeypatch)
    payload = {
        "repository": {"full_name": "acme/widget"},
        "sha": "0f3a2c1d",
        "state": state,
        "branches": [{"name": "agent/eng-1"}, {"name": "agent/eng-1-2"}],
    }

    await _events(tmp_path, payload=payload).on_status()

    [(event_type, published_event)] = published
    assert event_type == "commit.status_updated"
    assert published_event["payload"] == {
        "head_sha": "0f3a2c1d",
        "branches": ["agent/eng-1", "agent/eng-1-2"],
        "state": state,
        "is_pending": is_pending,
    }

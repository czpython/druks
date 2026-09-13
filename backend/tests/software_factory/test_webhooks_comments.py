from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from druks.contrib.software_factory import subscribers
from druks.contrib.software_factory.models import ProjectRepo
from druks.contrib.software_factory.workflows import PullRequestReview
from druks.core.webhooks.github import GitHubEvents
from druks.testing import make_settings


@pytest.fixture
def reviewer(monkeypatch):
    client = SimpleNamespace(
        get_mention_handle=AsyncMock(return_value="reviewer"), react_to_comment=AsyncMock()
    )
    monkeypatch.setattr(
        subscribers, "get_review_actor", AsyncMock(return_value=SimpleNamespace(client=client))
    )
    monkeypatch.setattr(ProjectRepo, "get_for_repo", AsyncMock(return_value=object()))
    monkeypatch.setattr(PullRequestReview, "dispatch", AsyncMock())
    return client


def _events(tmp_path, *, sender_type="User", body="@reviewer please review", **data):
    events = GitHubEvents(request=SimpleNamespace(), kwargs={}, settings=make_settings(tmp_path))
    events._data_cached = {
        "repository": {"full_name": "acme/widget"},
        "sender": {"type": sender_type, "login": "writer"},
        "comment": {"id": 99, "author_association": "OWNER", "body": body},
        **data,
    }
    return events


@pytest.mark.parametrize(
    "sender_type,body,is_dispatched",
    [
        ("User", "@Reviewer please review again", True),
        ("Bot", "@reviewer please review again", False),
        ("User", "Mail ops@reviewer.dev or ask @reviewer-team", False),
    ],
)
async def test_review_comment_mention_asks_for_a_review(
    tmp_path, reviewer, sender_type, body, is_dispatched
):
    events = _events(tmp_path, sender_type=sender_type, body=body, pull_request={"number": 7})

    await events.on_pull_request_review_comment_created()

    if is_dispatched:
        PullRequestReview.dispatch.assert_awaited_once_with(
            repo="acme/widget", pr_number=7, requested_by="writer"
        )
        reviewer.react_to_comment.assert_awaited_once_with(
            "acme/widget", 99, is_review_comment=True, content="eyes", fail_silently=True
        )
    else:
        PullRequestReview.dispatch.assert_not_awaited()
        reviewer.react_to_comment.assert_not_awaited()


async def test_a_conversation_mention_reacts_on_the_issue_comment(tmp_path, reviewer):
    events = _events(tmp_path, issue={"number": 7, "pull_request": {}})

    await events.on_issue_comment_created()

    reviewer.react_to_comment.assert_awaited_once_with(
        "acme/widget", 99, is_review_comment=False, content="eyes", fail_silently=True
    )

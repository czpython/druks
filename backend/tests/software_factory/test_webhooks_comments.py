from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from druks.contrib.software_factory import subscribers
from druks.contrib.software_factory.models import ProjectRepo
from druks.contrib.software_factory.workflows import PullRequestReview
from druks.core.webhooks.github import GitHubEvents
from druks.testing import make_settings


@pytest.mark.parametrize(
    "sender_type,body,is_dispatched,note",
    [
        ("User", "@Reviewer please review again", True, "please review again"),
        ("User", "  @Reviewer  ", True, ""),
        ("Bot", "@reviewer please review again", False, ""),
        ("User", "Mail ops@reviewer.dev or ask @reviewer-team", False, ""),
    ],
)
async def test_review_comment_mention_asks_for_a_review(
    tmp_path, monkeypatch, sender_type, body, is_dispatched, note
):
    actor = SimpleNamespace(
        client=SimpleNamespace(get_mention_handle=AsyncMock(return_value="reviewer"))
    )
    monkeypatch.setattr(subscribers, "get_review_actor", AsyncMock(return_value=actor))
    monkeypatch.setattr(ProjectRepo, "get_for_repo", AsyncMock(return_value=object()))
    dispatch = AsyncMock()
    monkeypatch.setattr(PullRequestReview, "dispatch", dispatch)
    events = GitHubEvents(request=SimpleNamespace(), kwargs={}, settings=make_settings(tmp_path))
    events._data_cached = {
        "repository": {"full_name": "acme/widget"},
        "pull_request": {"number": 7},
        "sender": {"type": sender_type, "login": "writer"},
        "comment": {"author_association": "OWNER", "body": body},
    }

    await events.on_pull_request_review_comment_created()

    if is_dispatched:
        dispatch.assert_awaited_once_with(
            repo="acme/widget",
            pr_number=7,
            requested_by="writer",
            note=note,
        )
    else:
        dispatch.assert_not_awaited()

from dataclasses import dataclass
from typing import Literal

from druks.core.apis.github import GitHubClient
from druks.core.services import Github

from .services import GithubReviewer


@dataclass(frozen=True)
class ReviewActor:
    """Who reviews act as, and how they may post. ``approve`` — the reviewer
    service, a distinct identity, so GitHub accepts its verdict reviews on
    operator-authored pull requests. ``comment`` — the operator itself, which
    GitHub bars from approving its own pull requests, so reviews publish as
    comment events with the verdict in the body."""

    service: type[Github]
    client: GitHubClient
    mode: Literal["approve", "comment"]


async def get_review_actor() -> ReviewActor:
    if await GithubReviewer.is_connected():
        return ReviewActor(
            service=GithubReviewer, client=await GithubReviewer.client(), mode="approve"
        )
    return ReviewActor(service=Github, client=await Github.client(), mode="comment")

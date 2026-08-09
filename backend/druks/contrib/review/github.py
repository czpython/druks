from dataclasses import dataclass
from typing import Literal

from druks.contrib.review.extension import Review
from druks.core.apis.github import GitHubClient, get_github_client
from druks.settings import load_settings


@dataclass(frozen=True)
class ReviewActor:
    """Who reviews act as, and how they may post. ``approve`` — a review
    identity distinct from the operator, so GitHub accepts its verdict reviews
    on operator-authored pull requests. ``comment`` — the operator itself,
    which GitHub bars from approving its own pull requests, so reviews publish
    as comment events with the verdict in the body."""

    client: GitHubClient
    mode: Literal["approve", "comment"]


def get_review_actor() -> ReviewActor:
    settings = Review.settings()
    if settings.app_id and settings.private_key:
        # Only a complete pair selects the distinct identity — a half-configured
        # one (flagged by clean()) still borrows the operator client below.
        return ReviewActor(
            client=GitHubClient(
                app_id=settings.app_id.get_secret_value(),
                private_key=settings.private_key.get_secret_value(),
                base_url=load_settings().github_api_url,
            ),
            mode="approve",
        )
    return ReviewActor(client=get_github_client(), mode="comment")

from dataclasses import dataclass
from typing import Literal

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
    settings = load_settings()
    if settings.github.reviewer_app_id and settings.github_reviewer_private_key_path:
        return ReviewActor(
            client=GitHubClient(
                app_id=settings.github.reviewer_app_id,
                private_key=settings.github_reviewer_private_key_path.read_text(),
                base_url=settings.github_api_url,
            ),
            mode="approve",
        )
    return ReviewActor(client=get_github_client(settings), mode="comment")

from druks.contrib.review.workflows import PullRequestReview
from druks.contrib.ship.models import ProjectRepo
from druks.core.apis.github import get_reviewer_github_client
from druks.settings import load_settings
from druks.signals import subscribe


@subscribe("pr.commented", payload__author_can_write=True)
async def mention_asks_for_a_review(*, repo: str, pr_number: int, payload: dict) -> None:
    """Addressing the reviewer app asks it to review that pull request, and only someone
    who writes to the repo may ask — a review is the account's to spend."""
    handle = await get_reviewer_github_client(load_settings()).get_mention_handle()
    is_mentioned = handle and f"@{handle}".casefold() in payload["body"].casefold()
    if is_mentioned and ProjectRepo.get_for_repo(repo):
        await PullRequestReview.dispatch(
            repo=repo, pr_number=pr_number, requested_by=payload["author"]
        )

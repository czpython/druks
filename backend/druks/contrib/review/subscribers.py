from druks.contrib.review.github import get_review_actor
from druks.contrib.review.workflows import PullRequestReview
from druks.contrib.software_factory.models import ProjectRepo
from druks.signals import subscribe


@subscribe("pr.commented", payload__author_can_write=True)
async def mention_asks_for_a_review(*, repo: str, pr_number: int, payload: dict) -> None:
    """Addressing the review actor asks it to review that pull request, and only someone
    who writes to the repo may ask — a review is the account's to spend."""
    handle = await (await get_review_actor()).client.get_mention_handle()
    is_mentioned = handle and f"@{handle}".casefold() in payload["body"].casefold()
    if is_mentioned and await ProjectRepo.get_for_repo(repo):
        await PullRequestReview.dispatch(
            repo=repo, pr_number=pr_number, requested_by=payload["author"]
        )

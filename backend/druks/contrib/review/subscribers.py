from druks.contrib.review.workflows import PullRequestReview
from druks.contrib.ship.models import ProjectRepo
from druks.core.apis.github import get_reviewer_github_client
from druks.settings import load_settings
from druks.signals import subscribe


@subscribe("pr.commented")
async def mention_asks_for_a_review(*, repo: str, pr_number: int, payload: dict) -> None:
    """Addressing the reviewer app in a comment asks it to review that pull request.
    A repo no project claims is left alone — there is nothing to review it against,
    and a webhook that shrugs is right where a request would be told no."""
    handle = await get_reviewer_github_client(load_settings()).get_mention_handle()
    is_mentioned = handle and f"@{handle}".casefold() in payload["body"].casefold()
    if is_mentioned and ProjectRepo.get_for_repo(repo):
        await PullRequestReview.dispatch(
            repo=repo, pr_number=pr_number, requested_by=payload["author"]
        )

from fastapi import APIRouter, Body, Depends, HTTPException, status

from druks.accounts.dependencies import current_account
from druks.accounts.models import Account
from druks.contrib.review.schemas import ReviewsResponse, ReviewSummary
from druks.contrib.review.workflows import PullRequestReview
from druks.contrib.ship.models import ProjectRepo
from druks.workflows import Subject

router = APIRouter(prefix="/pull-requests", tags=["review"])


@router.get("", response_model=ReviewsResponse, response_model_by_alias=True)
async def list_reviews() -> ReviewsResponse:
    """The reviews still going or stopped on a failure. A finished one lives on its
    pull request, so it leaves this list as soon as it has something to show there."""
    return ReviewsResponse(
        reviews=[
            ReviewSummary.from_subject(subject) for subject in Subject.list_open("pull_request")
        ]
    )


@router.post("", status_code=status.HTTP_202_ACCEPTED)
async def request_review(
    repo: str = Body(..., embed=True),
    pr_number: int = Body(..., embed=True, alias="prNumber"),
    account: Account = Depends(current_account),
) -> None:
    if not ProjectRepo.get_for_repo(repo):
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"{repo} is not a registered project repo — add it to a project first",
        )
    await PullRequestReview.dispatch(repo=repo, pr_number=pr_number, requested_by=account.username)

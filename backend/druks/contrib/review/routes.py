from fastapi import APIRouter, Body, Depends, HTTPException, status

from druks.accounts.dependencies import current_account
from druks.accounts.models import Account
from druks.contrib.review.schemas import ReviewRequestedResponse
from druks.contrib.review.workflows import PullRequestReview
from druks.contrib.ship.models import ProjectRepo

router = APIRouter(prefix="/reviews")


@router.post(
    "",
    status_code=status.HTTP_202_ACCEPTED,
    operation_id="review_request",
    tags=["agent"],
    response_model=ReviewRequestedResponse,
    response_model_by_alias=True,
    responses={
        404: {
            "description": "The repository is not registered.",
            "content": {
                "application/json": {
                    "example": {
                        "error": "HTTP_404",
                        "detail": (
                            "owner/name is not a registered project repo — "
                            "add it to a project first"
                        ),
                    }
                }
            },
        }
    },
)
async def request_review(
    repo: str = Body(
        ...,
        embed=True,
        description="A registered project repository in owner/name form.",
    ),
    pr_number: int = Body(
        ...,
        embed=True,
        alias="prNumber",
        gt=0,
        description="The pull request number in that repository.",
    ),
    account: Account = Depends(current_account),
) -> ReviewRequestedResponse:
    """Start a pull request review. The returned run feeds the run tools and
    is what list_open_subjects shows for the pull request; requesting again
    while the review is live returns the same run."""
    if not ProjectRepo.get_for_repo(repo):
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"{repo} is not a registered project repo — add it to a project first",
        )
    run_id = await PullRequestReview.dispatch(
        repo=repo,
        pr_number=pr_number,
        requested_by=account.username,
    )
    return ReviewRequestedResponse(run=run_id)

from fastapi import APIRouter, Body, Depends, HTTPException, status

from druks.accounts.dependencies import current_account
from druks.accounts.models import Account
from druks.contrib.review.workflows import PullRequestReview
from druks.contrib.ship.models import ProjectRepo

router = APIRouter(prefix="/pull-requests", tags=["review"])


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

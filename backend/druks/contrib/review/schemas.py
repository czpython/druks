from datetime import datetime

from druks.schemas import BaseResponse
from druks.workflows import RunResponse, Subject, SubjectStatus


class ReviewSummary(BaseResponse):
    repo: str
    pr_number: int
    pull_request_url: str
    # When this review was asked for and by whom — for review a dispatch is one
    # run, so its run answers both.
    triggered_at: datetime
    requested_by: str
    status: SubjectStatus

    @classmethod
    def from_subject(
        cls, subject: Subject, *, status: SubjectStatus, latest_run: RunResponse
    ) -> "ReviewSummary":
        repo, _, pr_number = subject.id.partition("#")
        return cls(
            repo=repo,
            pr_number=int(pr_number),
            pull_request_url=f"https://github.com/{repo}/pull/{pr_number}",
            triggered_at=latest_run.created_at,
            requested_by=latest_run.account_username,
            status=status,
        )


class ReviewsResponse(BaseResponse):
    reviews: list[ReviewSummary]

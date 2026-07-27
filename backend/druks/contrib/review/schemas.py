from datetime import datetime

from druks.schemas import BaseResponse
from druks.workflows import Subject


class ReviewSummary(BaseResponse):
    repo: str
    pr_number: int
    pull_request_url: str
    # Where the review got to, why it stopped, when it was asked for and by whom —
    # its status answers the whole row. ``state`` is the platform's, verbatim; the
    # words are the client's.
    state: str
    failure: str | None
    triggered_at: datetime
    requested_by: str

    @classmethod
    def from_subject(cls, subject: Subject) -> "ReviewSummary":
        repo, _, pr_number = subject.id.partition("#")
        status = subject.get_status()
        return cls(
            repo=repo,
            pr_number=int(pr_number),
            pull_request_url=f"https://github.com/{repo}/pull/{pr_number}",
            state=status.state,
            failure=status.failure,
            triggered_at=status.triggered_at,
            requested_by=status.account_username,
        )


class ReviewsResponse(BaseResponse):
    reviews: list[ReviewSummary]

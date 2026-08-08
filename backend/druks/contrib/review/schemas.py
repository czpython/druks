from druks.schemas import BaseResponse
from druks.workflows import SubjectSummary


class ReviewRequestedResponse(BaseResponse):
    run: str


class ReviewSummary(SubjectSummary):
    # The pull request's domain header — its handle, spelled out. Status (where the
    # review stands) and the timeline come from the platform's subject read-side.
    repo: str
    pr_number: int
    pull_request_url: str

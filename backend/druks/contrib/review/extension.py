from druks.agents import Agent
from druks.contrib.review.contracts import ReviewReport
from druks.contrib.review.schemas import ReviewSummary
from druks.extensions import Extension
from druks.workflows import Subject


class Review(Extension):
    name = "review"
    subject_type = "pull_request"
    icon = "git-pull-request"
    description = (
        "Reviews a pull request and posts the review back to GitHub — the decision, "
        "a summary, and a comment on each line it has something to say about."
    )

    review_pull_request = Agent(
        description="reads a pull request and writes the review",
        prompt="review/review_pull_request.md",
        contract=ReviewReport,
        model="claude",
    )

    @classmethod
    def get_subject_summary(cls, subject: Subject) -> ReviewSummary | None:
        # A pull request is a subject by identity alone: any id shaped like one names
        # one, whether or not druks has reviewed it, and the timeline says which.
        return ReviewSummary.from_subject(subject)

    @classmethod
    def list_subjects(cls) -> list[ReviewSummary]:
        """The reviews still going or stopped on a failure. A finished one lives on its
        pull request, so it leaves the board as soon as it has something to show there."""
        summaries = (cls.get_subject_summary(s) for s in Subject.list_open(cls.subject_type))
        return [summary for summary in summaries if summary]

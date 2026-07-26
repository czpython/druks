from druks.agents import Agent
from druks.contrib.review.contracts import ReviewReport
from druks.extensions import Extension


class Review(Extension):
    name = "review"
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

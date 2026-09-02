from pydantic import Field

from druks.agents import Agent
from druks.apps import App, AppSettings, Secret
from druks.contrib.review.contracts import ReviewReport
from druks.doctor import CheckResult


async def check_review_identity() -> CheckResult:
    """Set or unset, both healthy: an empty pair is comment mode by design.
    A half-configured pair is the settings clean's failure (``review:settings``),
    not this check's."""
    settings = await Review.settings()
    if settings.app_id and settings.private_key:
        return CheckResult(
            name="identity", ok=True, detail="set — reviews approve as the distinct App"
        )
    return CheckResult(name="identity", ok=True, detail="unset — reviews post as operator comments")


class Review(App):
    name = "review"
    icon = "git-pull-request"
    description = (
        "Reviews a pull request and posts the review back to GitHub — the decision, "
        "a summary, and a comment on each line it has something to say about."
    )

    class Settings(AppSettings):
        # The optional distinct review identity. Empty pair — reviews borrow
        # the operator client in comment mode; complete pair — verdict reviews
        # post as this App. App-owned by design: a posting identity for
        # one app is not a platform service identity.
        app_id: Secret = Field(
            title="Review App ID",
            description="GitHub App ID of the distinct review identity; empty posts as comments.",
        )
        private_key: Secret = Field(
            title="Review App private key",
            description="PEM private key of the review App, pasted as issued.",
            json_schema_extra={"multiline": True},
        )

        def clean(self) -> dict[str, str]:
            problems: dict[str, str] = {}
            if self.app_id and not self.private_key:
                problems["private_key"] = "Required once the review App ID is set."
            if self.private_key and not self.app_id:
                problems["app_id"] = "Required once the review App private key is set."
            return problems

    checks = [check_review_identity]

    review_pull_request = Agent(
        description="reads a pull request and writes the review",
        prompt="review/review_pull_request.md",
        contract=ReviewReport,
    )

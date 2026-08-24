from typing import Self

from druks.contrib.review.schemas import ReviewSummary
from druks.workflows import Subject


class PullRequest(Subject):
    """The pull request a review is about. Druks keeps no row for one, so the handle
    is the whole record — ``owner/repo#7`` is what everything else reads back out of,
    and its status carries the lifecycle."""

    @classmethod
    def get(cls, repo: str, number: int) -> Self:
        return cls(id=f"{repo}#{number}")

    @classmethod
    async def get_for_subject_id(cls, subject_id: str) -> Self | None:
        # Ids reach the read side as free text off a URL, so a shape that names no
        # pull request is a miss rather than a crashed read.
        repo, _, number = subject_id.partition("#")
        owner, _, name = repo.partition("/")
        if owner and name and number.isdigit() and int(number) > 0:
            return cls(id=subject_id)
        return

    @property
    def repo(self) -> str:
        return self.id.partition("#")[0]

    @property
    def number(self) -> int:
        return int(self.id.partition("#")[2])

    @property
    def url(self) -> str:
        return f"https://github.com/{self.repo}/pull/{self.number}"

    def get_summary(self) -> ReviewSummary:
        return ReviewSummary(
            id=self.id,
            label=self.label,
            repo=self.repo,
            pr_number=self.number,
            pull_request_url=self.url,
        )

    @classmethod
    async def list_summaries(cls, account_id: str | None) -> list[ReviewSummary]:
        """The reviews still going or stopped on a failure. A finished one lives on its
        pull request, so it leaves the board as soon as it has something to show there."""
        return [pull_request.get_summary() for pull_request in await cls.list_open()]

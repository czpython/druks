from dataclasses import dataclass
from typing import Any, Self

from druks.contrib.software_factory.constants import CI_LOG_LINES
from druks.contrib.software_factory.schemas import ReviewSummary
from druks.core.apis.github import GitHubClient
from druks.workflows import Subject

# The conclusions GitHub reports for a check that passed or did not apply.
_GREEN_CONCLUSIONS = frozenset({"success", "neutral", "skipped"})


class PullRequest(Subject):
    """The pull request a review is about. Druks keeps no row for one, so the handle
    is the whole record — ``owner/repo#7`` is what everything else reads back out of,
    and its status carries the lifecycle."""

    @classmethod
    async def get_or_none(cls, id: str) -> Self | None:
        # Ids reach the read side as free text off a URL, so a shape that names no
        # pull request is a miss rather than a crashed read.
        repo, _, number = id.partition("#")
        owner, _, name = repo.partition("/")
        if owner and name and number.isdigit() and int(number) > 0:
            return cls(id=id)
        return

    # The reviewer checks out the PR itself; the box starts on the default branch.
    ref = None

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
            key=self.key,
            repo=self.repo,
            pr_number=self.number,
            pull_request_url=self.url,
        )

    @classmethod
    async def list_summaries(cls, account_id: str | None) -> list[ReviewSummary]:
        """The reviews still going or stopped on a failure. A finished one lives on its
        pull request, so it leaves the board as soon as it has something to show there."""
        return [pull_request.get_summary() for pull_request in await cls.list_open()]


@dataclass(frozen=True)
class Check:
    """One check on a commit: a check run or a commit status."""

    name: str
    is_finished: bool
    is_green: bool
    url: str
    # A GitHub Actions job, whose log GitHub serves. Any other check reports in its output.
    job_id: int | None
    output: str

    @classmethod
    async def list_for_ref(cls, github: GitHubClient, repo: str, ref: str) -> list["Check"]:
        runs = await github.list_check_runs(repo, ref)
        statuses = await github.list_commit_statuses(repo, ref)
        return [
            *(cls.from_check_run(run) for run in runs),
            *(cls.from_status(status) for status in statuses),
        ]

    @classmethod
    def from_check_run(cls, run: dict[str, Any]) -> "Check":
        output = run["output"]
        return cls(
            name=run["name"],
            is_finished=run["status"] == "completed",
            is_green=run["conclusion"] in _GREEN_CONCLUSIONS,
            url=run["details_url"] or run["html_url"],
            job_id=run["id"] if run["app"]["slug"] == "github-actions" else None,
            output="\n\n".join(part for part in (output["summary"], output["text"]) if part),
        )

    @classmethod
    def from_status(cls, status: dict[str, Any]) -> "Check":
        return cls(
            name=status["context"],
            is_finished=status["state"] != "pending",
            is_green=status["state"] == "success",
            url=status["target_url"] or "",
            job_id=None,
            output=status["description"] or "",
        )

    async def get_log(self, github: GitHubClient, repo: str) -> str:
        log = await github.get_job_log(repo, self.job_id) if self.job_id else self.output
        return "\n".join(log.splitlines()[-CI_LOG_LINES:])

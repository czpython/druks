from typing import Any

from druks.accounts.models import Account
from druks.contrib.review.app import Review
from druks.contrib.review.datastructures import PullRequest
from druks.contrib.review.github import get_review_actor
from druks.contrib.software_factory.models import ProjectRepo
from druks.core.apis.github import GITHUB
from druks.sandbox.layout import get_related_root
from druks.services.models import ServiceIdentity
from druks.workflows import Workflow
from druks.workspaces import RepoWorkspace


class ReviewWorkspace(RepoWorkspace):
    # The default-branch checkout (the reviewer checks the PR out itself) plus room
    # beside it for siblings; Claude's add_dirs grant needs the directory to exist.

    @property
    def related_root(self) -> str:
        return get_related_root(self.host.ssh_username)

    async def get_github_token(self) -> str:
        # The review is authored under the review actor's identity.
        return await (await get_review_actor()).client.token_for_repo(self.get_repo())

    async def run_agent(self, *, account_id: str | None, **kwargs: Any):
        await self.host.exec(["mkdir", "-p", self.related_root], timeout=10.0)
        return await super().run_agent(account_id=account_id, **kwargs)

    def get_agent_run_kwargs(self, **kwargs: Any) -> dict[str, Any]:
        kwargs = super().get_agent_run_kwargs(**kwargs)
        kwargs["add_dirs"] = (self.related_root,)
        return kwargs


class PullRequestReview(Workflow):
    """Reviews one pull request against a checkout of the repo it targets and the
    repos around it; the reviewer reads, judges, and posts the review itself."""

    subject = PullRequest
    workspace_class = ReviewWorkspace

    @classmethod
    async def dispatch(cls, *, repo: str, pr_number: int, requested_by: str) -> str:
        # Even a distinct review identity clones alongside the operator App, so
        # resolve the operator identity before the start spends a run and
        # provisions a VM — the raising lookup surfaces the actionable error.
        await ServiceIdentity.get(GITHUB)
        # Attribution follows the requester when druks knows them by that name; a
        # review asked for by someone with no account runs as the system's.
        account = await Account.get_for_username(requested_by)
        return await cls.start(
            subject=PullRequest.get(repo, pr_number),
            account_id=account.id if account else None,
            requested_by=requested_by,
        )

    async def run(self, requested_by: str) -> None:
        await Review.review_pull_request()

    async def get_prompt_context(self, **context: Any) -> dict[str, Any]:
        target = await ProjectRepo.get_for_repo((await self.subject).repo, raise_on_missing=True)
        return {
            "siblings": await target.siblings(),
            "review_mode": (await get_review_actor()).mode,
            **await super().get_prompt_context(**context),
        }

from typing import TYPE_CHECKING, Any

from druks.accounts.models import Account
from druks.contrib.review.datastructures import PullRequest
from druks.contrib.review.extension import Review
from druks.contrib.review.github import get_review_actor
from druks.contrib.ship.models import ProjectRepo
from druks.contrib.ship.workspace import RepoWorkspace
from druks.core.apis.github import GITHUB
from druks.sandbox import repo as _repo
from druks.sandbox.layout import get_github_token_remote_path, get_related_root, get_repo_root
from druks.services.models import ServiceIdentity
from druks.workflows import Workflow

if TYPE_CHECKING:
    from druks.sandbox.host import Sandbox


class ReviewWorkspace(RepoWorkspace):
    # The target repo's checkout, plus room beside it for the siblings the reviewer
    # decides to open. Claude scopes file access to cwd + add_dirs, so the related
    # root is granted before anything is cloned into it.

    @property
    def related_root(self) -> str:
        return get_related_root(self.sandbox.ssh_username)

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
        ServiceIdentity.get(GITHUB)
        # Attribution follows the requester when druks knows them by that name; a
        # review asked for by someone with no account runs as the system's.
        account = Account.get_for_username(requested_by)
        return await cls.start(
            subject=PullRequest.get(repo, pr_number),
            account_id=account.id if account else None,
            requested_by=requested_by,
        )

    async def run(self, requested_by: str) -> None:
        await Review.review_pull_request()

    async def get_workspace_kwargs(self, sandbox: "Sandbox") -> dict[str, Any]:
        # Cloned at the default branch — the reviewer checks the pull request out itself.
        # The token is the review actor's: it authenticates the clone and ``gh``, so the
        # review is authored under that identity. Siblings stay uncloned — the reviewer
        # clones the ones it opens, into a directory that must exist for the grant to hold.
        repo = self.subject.repo
        github_token = await get_review_actor().client.token_for_repo(repo)
        await sandbox.write_secret(
            secret=github_token, remote=get_github_token_remote_path(sandbox.ssh_username)
        )
        await _repo.ensure(
            sandbox,
            repo_url=f"https://github.com/{repo}",
            ref=None,
            target_path=get_repo_root(sandbox.ssh_username),
        )
        await sandbox.exec(["mkdir", "-p", get_related_root(sandbox.ssh_username)], timeout=10.0)
        return {
            **await super().get_workspace_kwargs(sandbox),
            "repo": repo,
            "github_token": github_token,
        }

    async def get_prompt_context(self, **context: Any) -> dict[str, Any]:
        target = ProjectRepo.get_for_repo(self.subject.repo, raise_on_missing=True)
        return {
            "siblings": target.siblings(),
            "review_mode": get_review_actor().mode,
            **await super().get_prompt_context(**context),
        }

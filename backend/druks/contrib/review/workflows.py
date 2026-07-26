from typing import TYPE_CHECKING, Any

from druks.contrib.review.extension import Review
from druks.contrib.ship.models import ProjectRepo
from druks.contrib.ship.workspace import RepoWorkspace
from druks.core.apis.github import get_reviewer_github_client
from druks.sandbox import repo as _repo
from druks.sandbox.layout import get_github_token_remote_path, get_related_root, get_repo_root
from druks.settings import load_settings
from druks.workflows import Subject, Workflow

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

    workspace_class = ReviewWorkspace

    @classmethod
    async def dispatch(cls, *, repo: str, pr_number: int, requested_by: str) -> str:
        return await cls.start(
            subject=Subject(id=f"{repo}#{pr_number}", subject_type="pull_request"),
            repo=repo,
            pr_number=pr_number,
            requested_by=requested_by,
        )

    async def run(self, repo: str, pr_number: int, requested_by: str) -> None:
        await Review.review_pull_request()

    async def get_workspace_kwargs(self, sandbox: "Sandbox") -> dict[str, Any]:
        # Cloned at the default branch — the reviewer checks the pull request out itself.
        # The token is the reviewer app's: it authenticates the clone and ``gh``, so the
        # review is authored under that identity. Siblings stay uncloned — the reviewer
        # clones the ones it opens, into a directory that must exist for the grant to hold.
        repo = self.input.repo
        github_token = await get_reviewer_github_client(load_settings()).token_for_repo(repo)
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
        target = ProjectRepo.get_for_repo(self.input.repo, raise_on_missing=True)
        return {
            "siblings": target.siblings(),
            **await super().get_prompt_context(**context),
        }

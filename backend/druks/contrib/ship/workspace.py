import shlex
from dataclasses import dataclass
from typing import Any

from druks.accounts.models import Account
from druks.core.apis.github import get_github_client
from druks.sandbox.datastructures import Workspace
from druks.sandbox.exceptions import ExecFailed
from druks.sandbox.layout import get_repo_root


@dataclass(frozen=True)
class RepoWorkspace(Workspace):
    # A VM with the target repo cloned in and a short-lived token its agent
    # pushes/reads through. Build's workspace extends this with the PR branch
    # and the github MCP token; the profiler uses it as-is.
    repo: str
    github_token: str

    @property
    def repo_path(self) -> str:
        return get_repo_root(self.sandbox.ssh_username)

    def get_agent_run_kwargs(self, **kwargs: Any) -> dict[str, Any]:
        kwargs["github_token"] = self.github_token
        return kwargs

    async def run_agent(self, *, account_id: str | None, **kwargs: Any):
        await self.set_git_identity(account_id)
        return await super().run_agent(account_id=account_id, **kwargs)

    async def set_git_identity(self, account_id: str | None) -> None:
        """Commits in the repo are authored as the operator's bot user, with a
        prepare-commit-msg hook crediting the account that dispatched the run.
        Rewritten before every agent call so a reused warm host follows the
        current run's dispatcher — a system dispatch carries no hook and
        credits nobody."""
        author_name, author_email = await get_github_client().get_bot_git_author()
        steps = [
            f"cd {shlex.quote(self.repo_path)}",
            f"git config user.name {shlex.quote(author_name)}",
            f"git config user.email {shlex.quote(author_email)}",
            "rm -f .git/hooks/prepare-commit-msg",
        ]
        if account_id and (account := Account.get(account_id, exclude_system=True)):
            trailer = f"Co-Authored-By: {account.username} <{account.username}>"
            hook = (
                "#!/bin/sh\n"
                f"git interpret-trailers --in-place --if-exists doNothing"
                f' --trailer {shlex.quote(trailer)} "$1"\n'
            )
            steps += [
                f"printf %s {shlex.quote(hook)} > .git/hooks/prepare-commit-msg",
                "chmod 755 .git/hooks/prepare-commit-msg",
            ]
        result = await self.sandbox.exec(["sh", "-c", " && ".join(steps)], timeout=10.0)
        if not result.ok:
            raise ExecFailed(
                f"failed to set the workspace git identity: "
                f"exit={result.exit_code} stderr={result.stderr.strip()}",
                exit_code=result.exit_code,
            )

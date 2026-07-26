from dataclasses import dataclass
from typing import Any

from druks.sandbox.datastructures import Workspace
from druks.sandbox.layout import get_repo_root


@dataclass(frozen=True)
class RepoWorkspace(Workspace):
    # A VM with the target repo cloned in and a short-lived token its agent
    # pushes/reads through. Build's workspace extends this with the PR branch
    # and the reviewer MCP token; the profiler uses it as-is.
    repo: str
    github_token: str

    @property
    def repo_path(self) -> str:
        return get_repo_root(self.sandbox.ssh_username)

    def get_agent_run_kwargs(self, **kwargs: Any) -> dict[str, Any]:
        kwargs["github_token"] = self.github_token
        return kwargs

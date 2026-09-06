from types import SimpleNamespace
from typing import Any

import pytest
from druks import workspaces as workspace_mod
from druks.sandbox.layout import get_github_token_remote_path, get_repo_root
from druks.workspaces import RepoWorkspace, Workspace


class _RecordingHost:
    id = "h1"
    ssh_username = "exedev"

    def __init__(self) -> None:
        self.events: list[tuple[Any, ...]] = []

    async def write_secret(self, *, secret: str, remote: str) -> None:
        self.events.append(("write_secret", remote, secret))


async def test_repo_workspace_mints_writes_and_clones_before_every_agent_call(
    monkeypatch: pytest.MonkeyPatch,
):
    host = _RecordingHost()
    subject = SimpleNamespace(repo="acme/widgets")
    workspace = RepoWorkspace(host=host, subject=subject, branch="feature")  # type: ignore[arg-type]

    async def _token(self: RepoWorkspace) -> str:
        return "ghs_fresh"

    async def _ensure(_host: Any, *, repo_url: str, ref: str | None, target_path: str) -> None:
        host.events.append(("clone", repo_url, ref, target_path))

    async def _git_identity(self: RepoWorkspace, account_id: str | None) -> None:
        host.events.append(("git_identity", account_id))

    async def _run(self: Workspace, *, account_id: str | None, **kwargs: Any) -> str:
        host.events.append(("run_agent", account_id, kwargs))
        return "result"

    monkeypatch.setattr(RepoWorkspace, "get_github_token", _token)
    monkeypatch.setattr(workspace_mod.checkout, "ensure", _ensure)
    monkeypatch.setattr(RepoWorkspace, "set_git_identity", _git_identity)
    monkeypatch.setattr(Workspace, "run_agent", _run)

    assert await workspace.run_agent(account_id="acct", prompt="p") == "result"
    assert host.events == [
        ("write_secret", get_github_token_remote_path("exedev"), "ghs_fresh"),
        ("clone", "https://github.com/acme/widgets", "feature", get_repo_root("exedev")),
        ("git_identity", "acct"),
        ("run_agent", "acct", {"github_token": "ghs_fresh", "prompt": "p"}),
    ]

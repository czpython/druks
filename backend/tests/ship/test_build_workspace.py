import shlex
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from druks.contrib.ship import workspace as workspace_mod
from druks.contrib.ship.constants import GITHUB_MCP_NAME, GITHUB_MCP_URL
from druks.contrib.ship.workflows import Build, BuildWorkspace
from druks.contrib.ship.workspace import RepoWorkspace
from druks.mcp.helpers import get_bearer_token_env_var
from druks.sandbox import host as host_mod
from druks.sandbox.layout import get_related_root, get_repo_root
from druks.workflows import FatalError


class _FakeSandbox:
    ssh_username = "exedev"


def test_build_workspace_grants_related_root_add_dir():
    # Agents clone related repos on demand; the whole get_related_root is the
    # file-tool grant, no per-repo threading. MCP delivery is the fold's job —
    # scaffolding kwargs never carry it.
    workspace = BuildWorkspace(
        sandbox=_FakeSandbox(),  # type: ignore[arg-type]
        repo="o/main",
        branch="b",
        github_token="t",
        mcp_token="ghs_review",
        skills=("python-house-rules",),
    )
    kwargs = workspace.get_agent_run_kwargs(model="m")

    assert kwargs["model"] == "m"  # the run's own kwargs pass through
    assert kwargs["add_dirs"] == (get_related_root("exedev"),)
    assert kwargs["github_token"] == "t"
    assert kwargs["skills"] == ("python-house-rules",)
    assert "mcp_servers" not in kwargs
    assert "extra_env" not in kwargs


async def test_build_workspace_declares_its_github_mcp(druks_db):
    # The github MCP is build's own declaration, credentialed with the per-repo
    # review-actor token — never an operator catalog entry, never optional (there
    # is no build without github). Delivery ships it whole: wire shape + token
    # in the run env.
    workspace = BuildWorkspace(
        sandbox=_FakeSandbox(),  # type: ignore[arg-type]
        repo="o/main",
        branch="b",
        github_token="t",
        mcp_token="ghs_review",
        skills=("python-house-rules",),
    )
    kwargs = await workspace.with_mcp_servers(None, **workspace.get_agent_run_kwargs())

    assert kwargs["extra_env"] == {get_bearer_token_env_var(GITHUB_MCP_NAME): "ghs_review"}
    github = next(s for s in kwargs["mcp_servers"] if s.name == GITHUB_MCP_NAME)
    assert github.url == GITHUB_MCP_URL
    assert "ghs_review" not in repr(github)


def _workspace_kwargs_stubs(monkeypatch: pytest.MonkeyPatch, *, review_actor):
    ensured: list[str] = []
    execs: list[list[str]] = []

    async def _token(_repo: str) -> str:
        return "tok"

    async def _noop(self: Any, **_kw: Any) -> None:
        pass

    async def fake_ensure(_sb: Any, *, repo_url: str, ref: Any, target_path: str) -> None:
        ensured.append(repo_url)

    async def fake_exec(self: Any, argv: list[str], **_kw: Any) -> Any:
        execs.append(argv)
        return SimpleNamespace(ok=True, exit_code=0, stdout="", stderr="")

    monkeypatch.setattr(host_mod.Sandbox, "write_secret", _noop)
    monkeypatch.setattr(host_mod.Sandbox, "exec", fake_exec)
    monkeypatch.setattr(
        "druks.contrib.ship.workflows.get_github_client",
        lambda: SimpleNamespace(token_for_repo=_token),
    )
    monkeypatch.setattr("druks.contrib.ship.workflows.get_review_actor", review_actor)
    monkeypatch.setattr("druks.sandbox.repo.ensure", fake_ensure)
    return ensured, execs


@pytest.mark.asyncio
async def test_get_workspace_kwargs_clones_primary_only(monkeypatch: pytest.MonkeyPatch):
    # Only the primary repo is provisioned; related repos are the agents' job.
    # get_related_root is mkdir'd so Claude's --add-dir target exists before the
    # first on-demand clone.
    async def _review_token(_repo: str) -> str:
        return "ghs_review"

    ensured, execs = _workspace_kwargs_stubs(
        monkeypatch,
        review_actor=lambda: SimpleNamespace(
            client=SimpleNamespace(token_for_repo=_review_token), mode="approve"
        ),
    )
    sandbox = host_mod.Sandbox(record=SimpleNamespace(id="h1", ssh_username="exedev"))  # type: ignore[arg-type]

    workflow = Build()
    workflow.input = Build._run_input_model()
    workflow.subject = SimpleNamespace(repo="o/extension")
    workflow._profile = {"recommended_skills": ["python-house-rules"]}
    kwargs = await workflow.get_workspace_kwargs(sandbox)

    assert ensured == ["https://github.com/o/extension"]
    assert ["mkdir", "-p", get_related_root("exedev")] in execs
    assert kwargs["mcp_token"] == "ghs_review"
    assert kwargs["skills"] == ("python-house-rules",)
    assert "related" not in kwargs


@pytest.mark.asyncio
async def test_get_workspace_kwargs_fails_loudly_when_the_token_wont_mint(
    monkeypatch: pytest.MonkeyPatch,
):
    # There is no build without github: a run that can't mint its MCP token
    # fails at workspace setup, never degrades mid-run.
    async def _no_token(_repo: str) -> str:
        raise RuntimeError("app not installed on this repo")

    _workspace_kwargs_stubs(
        monkeypatch,
        review_actor=lambda: SimpleNamespace(
            client=SimpleNamespace(token_for_repo=_no_token), mode="comment"
        ),
    )
    sandbox = host_mod.Sandbox(record=SimpleNamespace(id="h1", ssh_username="exedev"))  # type: ignore[arg-type]

    workflow = Build()
    workflow.input = Build._run_input_model()
    workflow.subject = SimpleNamespace(repo="o/extension")
    workflow._profile = {"recommended_skills": ["python-house-rules"]}

    with pytest.raises(FatalError, match="github MCP server"):
        await workflow.get_workspace_kwargs(sandbox)


class _IdentitySandbox:
    ssh_username = "exedev"

    def __init__(self, repo_path: Path) -> None:
        self.repo_path = repo_path

    async def exec(self, command: list[str], *, timeout: float = 30.0) -> Any:
        del timeout
        local = command[2].replace(
            get_repo_root(self.ssh_username), shlex.quote(str(self.repo_path))
        )
        result = subprocess.run(["sh", "-c", local], check=False, capture_output=True, text=True)
        return SimpleNamespace(ok=result.returncode == 0, exit_code=result.returncode, stderr="")


def _dispatched_by(monkeypatch: pytest.MonkeyPatch, username: str | None) -> None:
    async def _bot_git_author() -> tuple[str, str]:
        return "app[bot]", "1+app[bot]@users.noreply.github.com"

    monkeypatch.setattr(
        workspace_mod,
        "get_github_client",
        lambda: SimpleNamespace(get_bot_git_author=_bot_git_author),
    )
    account = SimpleNamespace(username=username) if username else None
    monkeypatch.setattr(
        workspace_mod,
        "Account",
        SimpleNamespace(get=lambda _id, *, exclude_system: account),
    )


async def test_set_git_identity_stamps_the_workspace_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_path = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo_path)], check=True)
    workspace = RepoWorkspace(sandbox=_IdentitySandbox(repo_path), repo="o/main", github_token="t")  # type: ignore[arg-type]
    hook = repo_path / ".git" / "hooks" / "prepare-commit-msg"
    message = repo_path / "COMMIT_EDITMSG"

    _dispatched_by(monkeypatch, "dev@example.com")
    await workspace.set_git_identity("account-1")
    message.write_text("Change\n")
    subprocess.run([str(hook), str(message), "squash"], check=True)
    subprocess.run([str(hook), str(message)], check=True)
    email = subprocess.run(
        ["git", "-C", str(repo_path), "config", "user.email"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert email.stdout.strip() == "1+app[bot]@users.noreply.github.com"
    assert message.read_text().count("Co-Authored-By: dev@example.com <dev@example.com>") == 1

    # A reused warm host follows the next run's dispatcher.
    _dispatched_by(monkeypatch, "second@example.com")
    await workspace.set_git_identity("account-2")
    message.write_text("Change\n")
    subprocess.run([str(hook), str(message)], check=True)
    assert "dev@example.com" not in message.read_text()
    assert "Co-Authored-By: second@example.com" in message.read_text()

    # A system dispatch keeps the author but credits nobody.
    _dispatched_by(monkeypatch, None)
    await workspace.set_git_identity(None)
    assert not hook.exists()

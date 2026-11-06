import shlex
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from conftest import connect_service
from druks.accounts.context import current_account_id
from druks.contrib.software_factory.app import SoftwareFactory
from druks.contrib.software_factory.constants import (
    GITHUB_MCP_NAME,
    GITHUB_MCP_URL,
    TICKET_TOOLS,
)
from druks.contrib.software_factory.models import Project, ProjectRepo
from druks.contrib.software_factory.workflows import BuildWorkspace, ReviewWorkspace
from druks.core.services import Github
from druks.mcp.helpers import get_bearer_token_env_var
from druks.sandbox import SandboxSecret
from druks.sandbox.host import Host
from druks.sandbox.layout import get_related_root, get_repo_root
from druks.settings import Urls
from druks.workspaces import RepoWorkspace


class _FakeSandbox:
    ssh_username = "exedev"


def test_build_workspace_grants_related_root_add_dir():
    workspace = BuildWorkspace(SimpleNamespace(repo="o/main"))
    workspace.host = _FakeSandbox()  # type: ignore[assignment]
    kwargs = workspace.get_agent_run_kwargs(model="m")

    assert kwargs["model"] == "m"
    assert kwargs["add_dirs"] == (get_related_root("exedev"),)
    assert "skills" not in kwargs
    assert "mcp_servers" not in kwargs
    assert "extra_env" not in kwargs


async def test_build_workspace_makes_the_related_root_before_the_clone(
    monkeypatch: pytest.MonkeyPatch,
):
    execs: list[list[str]] = []

    async def fake_exec(self: Any, argv: list[str], **_: Any) -> None:
        execs.append(argv)

    async def base_prepare(self: Any) -> None:
        execs.append(["prepare"])

    workspace = BuildWorkspace(SimpleNamespace(repo="o/main"))
    workspace.host = Host(record=SimpleNamespace(id="h1", ssh_username="exedev"))  # type: ignore[arg-type]
    monkeypatch.setattr(Host, "exec", fake_exec)
    monkeypatch.setattr(RepoWorkspace, "prepare", base_prepare)

    await workspace.prepare()
    assert execs == [["mkdir", "-p", get_related_root("exedev")], ["prepare"]]


async def test_build_workspace_declares_its_github_mcp_as_the_operator(druks_db):
    # The review runs as the reviewer in its own workflow, so a build never acts as it.
    operator = await connect_service(
        "github", identity={"app_id": "1", "slug": "druks-operator"}, secrets={"private_key": "pem"}
    )
    await connect_service(
        "github_reviewer",
        identity={"app_id": "2", "slug": "druks-reviewer"},
        secrets={"private_key": "reviewer-pem"},
    )
    subject = SimpleNamespace(repo="o/main")

    [github], [ref] = await BuildWorkspace.get_all_mcp_servers(druks_db, subject, None)

    assert github.url == GITHUB_MCP_URL
    assert github.bearer_token_env_var == get_bearer_token_env_var(GITHUB_MCP_NAME)
    assert ref.key == ("mcp_github_token", operator.id, ("o/main",), "api.githubcopilot.com")
    [clone] = await BuildWorkspace.get_secrets(subject)
    assert clone == SandboxSecret(
        name="github", secret_id=operator.id, resources=("o/main",), host="github.com"
    )


async def test_a_build_gets_the_skills_its_repo_profile_recommends(monkeypatch: pytest.MonkeyPatch):
    async def get_for_repo(repo: str, *, raise_on_missing: bool = False) -> SimpleNamespace:
        return SimpleNamespace(effective_profile={"recommended_skills": ["python-house-rules"]})

    monkeypatch.setattr(
        "druks.contrib.software_factory.workflows.ProjectRepo.get_for_repo", get_for_repo
    )

    skills = await BuildWorkspace.get_skills(SimpleNamespace(repo="o/app"))

    assert skills == ("python-house-rules",)


async def _required_servers(monkeypatch: pytest.MonkeyPatch, tracker: str):
    await connect_service(
        "github", identity={"app_id": "1", "slug": "druks-operator"}, secrets={"private_key": "pem"}
    )
    monkeypatch.setattr(
        "druks.mcp.inbound.get_settings",
        lambda: SimpleNamespace(urls=Urls(endpoint="https://druks.test", webhook_host="")),
    )
    settings = SoftwareFactory.Settings(tracker=tracker)
    monkeypatch.setattr(SoftwareFactory, "settings", AsyncMock(return_value=settings))
    return await BuildWorkspace.get_mcp_servers(SimpleNamespace(repo="o/main"))


async def test_a_board_build_requires_the_appliance_mcp(druks_db, monkeypatch):
    servers = await _required_servers(monkeypatch, "druks")

    assert [server.name for server in servers] == [GITHUB_MCP_NAME, "druks"]
    board = servers[-1]
    assert (board.url, board.secret_id, board.allowed_tools) == (
        "https://druks.test/mcp",
        "",
        TICKET_TOOLS,
    )


async def test_a_linear_build_requires_github_alone(druks_db, monkeypatch):
    servers = await _required_servers(monkeypatch, "linear")

    assert [server.name for server in servers] == [GITHUB_MCP_NAME]


def test_the_build_clones_as_the_operator():
    assert BuildWorkspace.github is Github


async def test_review_mcp_and_gh_use_the_review_actor(druks_db):
    await connect_service(
        "github", identity={"app_id": "1", "slug": "druks-operator"}, secrets={"private_key": "pem"}
    )
    reviewer = await connect_service(
        "github_reviewer",
        identity={"app_id": "2", "slug": "druks-reviewer"},
        secrets={"private_key": "reviewer-pem"},
    )
    subject = SimpleNamespace(repo="o/app")

    [github], [ref] = await ReviewWorkspace.get_all_mcp_servers(druks_db, subject, None)

    assert github.url == GITHUB_MCP_URL
    assert github.bearer_token_env_var == get_bearer_token_env_var(GITHUB_MCP_NAME)
    assert ref.key == ("mcp_github_token", reviewer.id, ("o/app",), "api.githubcopilot.com")
    [clone] = await ReviewWorkspace.get_secrets(subject)
    assert clone == SandboxSecret(
        name="github_reviewer", secret_id=reviewer.id, resources=("o/app",), host="github.com"
    )


@pytest.mark.parametrize("workspace_class", [BuildWorkspace, ReviewWorkspace])
async def test_the_github_token_reaches_the_project_siblings(druks_db, workspace_class):
    await connect_service(
        "github", identity={"app_id": "1", "slug": "druks-operator"}, secrets={"private_key": "pem"}
    )
    project = await Project.create(name="Acme")
    await ProjectRepo.create(project_id=project.id, full_name="acme/api")
    await ProjectRepo.create(project_id=project.id, full_name="acme/web")
    await druks_db.flush()

    [clone] = await workspace_class.get_secrets(SimpleNamespace(repo="acme/api"))

    assert clone.resources == ("acme/api", "acme/web")


class _IdentitySandbox:
    ssh_username = "exedev"

    def __init__(self, repo_path: Path) -> None:
        self.repo_path = repo_path

    async def exec(self, command: list[str], *, timeout: float = 30.0) -> Any:
        script = command[2].replace(
            get_repo_root(self.ssh_username), shlex.quote(str(self.repo_path))
        )
        result = subprocess.run(["sh", "-c", script], check=False, capture_output=True, text=True)
        return SimpleNamespace(returncode=result.returncode, stderr="")


def _bot_author(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _bot_git_author() -> tuple[str, str]:
        return "app[bot]", "1+app[bot]@users.noreply.github.com"

    async def _client(repo):
        return SimpleNamespace(get_bot_git_author=_bot_git_author)

    monkeypatch.setattr("druks.core.services.Github.get_client", _client)


async def test_set_git_identity_stamps_the_workspace_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_path = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo_path)], check=True)
    _bot_author(monkeypatch)

    async def get_or_none(*, id: str) -> SimpleNamespace:
        return SimpleNamespace(username=id)

    monkeypatch.setattr("druks.workspaces.Account.get_or_none", get_or_none)
    workspace = RepoWorkspace(SimpleNamespace(repo="o/app"))
    workspace.host = _IdentitySandbox(repo_path)  # type: ignore[assignment]
    dispatch = current_account_id.set("dev@example.com")
    hook = repo_path / ".git" / "hooks" / "prepare-commit-msg"
    message = repo_path / "COMMIT_EDITMSG"

    await workspace.set_git_identity()
    # A co-author line already in the message does not take the dispatcher's place.
    message.write_text("Change\n\nCo-Authored-By: Dana <dana@example.com>\n")
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
    current_account_id.set("second@example.com")
    await workspace.set_git_identity()
    message.write_text("Change\n")
    subprocess.run([str(hook), str(message)], check=True)
    assert "dev@example.com" not in message.read_text()
    assert "Co-Authored-By: second@example.com" in message.read_text()

    # A system dispatch keeps the author but credits nobody.
    current_account_id.set(None)
    await workspace.set_git_identity()
    assert not hook.exists()
    current_account_id.reset(dispatch)

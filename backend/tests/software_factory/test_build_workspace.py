import shlex
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from conftest import connect_service
from druks import workspaces as workspace_mod
from druks.contrib.software_factory.app import SoftwareFactory
from druks.contrib.software_factory.constants import (
    GITHUB_MCP_NAME,
    GITHUB_MCP_URL,
    TICKET_TOOLS,
)
from druks.contrib.software_factory.services import GithubReviewer
from druks.contrib.software_factory.workflows import Build, BuildWorkspace, ReviewWorkspace
from druks.core.services import Github
from druks.mcp.helpers import get_bearer_token_env_var
from druks.sandbox import host as host_mod
from druks.sandbox.layout import get_related_root, get_repo_root
from druks.workspaces import RepoWorkspace


class _FakeSandbox:
    ssh_username = "exedev"


def test_build_workspace_grants_related_root_add_dir():
    # Agents clone related repos on demand; the whole get_related_root is the
    # file-tool grant, no per-repo threading. MCP delivery is the fold's job —
    # scaffolding kwargs never carry it.
    workspace = BuildWorkspace(
        host=_FakeSandbox(),  # type: ignore[arg-type]
        subject=SimpleNamespace(repo="o/main"),
        branch="b",
        skills=("python-house-rules",),
    )
    kwargs = workspace.get_agent_run_kwargs(model="m")

    assert kwargs["model"] == "m"  # the run's own kwargs pass through
    assert kwargs["add_dirs"] == (get_related_root("exedev"),)
    assert kwargs["skills"] == ("python-house-rules",)
    assert "mcp_servers" not in kwargs
    assert "extra_env" not in kwargs


async def test_build_workspace_makes_the_related_root_before_the_clone(
    monkeypatch: pytest.MonkeyPatch,
):
    execs: list[list[str]] = []

    async def fake_exec(self: Any, argv: list[str], **_kw: Any) -> None:
        execs.append(argv)

    async def base_run_agent(self: Any, **kwargs: Any) -> str:
        execs.append(["run_agent"])
        return "ran"

    workspace = BuildWorkspace(
        host=host_mod.Host(record=SimpleNamespace(id="h1", ssh_username="exedev")),  # type: ignore[arg-type]
        subject=SimpleNamespace(repo="o/main"),
        branch="b",
        skills=(),
    )
    monkeypatch.setattr(host_mod.Host, "exec", fake_exec)
    monkeypatch.setattr(RepoWorkspace, "run_agent", base_run_agent)

    assert await workspace.run_agent(account_id=None) == "ran"
    assert execs == [["mkdir", "-p", get_related_root("exedev")], ["run_agent"]]


async def test_build_workspace_declares_its_github_mcp_as_the_review_actor(druks_db):
    # The github MCP is build's own declaration, issued through the review
    # actor's vault row for the subject's repo — never an operator catalog
    # entry, never optional (there is no build without github). The clone
    # stays the operator's: two identities, two entries in the box.
    operator = await connect_service(
        "github", identity={"app_id": "1", "slug": "druks-operator"}, secrets={"private_key": "pem"}
    )
    reviewer = await connect_service(
        "github_reviewer",
        identity={"app_id": "2", "slug": "druks-reviewer"},
        secrets={"private_key": "reviewer-pem"},
    )
    subject = SimpleNamespace(repo="o/main")

    wire, refs = await BuildWorkspace.get_mcp_delivery(subject, None)

    github = next(s for s in wire if s.name == GITHUB_MCP_NAME)
    assert github.url == GITHUB_MCP_URL
    assert github.bearer_token_env_var == get_bearer_token_env_var(GITHUB_MCP_NAME)
    [ref] = refs
    assert ref.key == ("mcp_github_token", reviewer.id, "o/main", "api.githubcopilot.com")
    [clone] = await BuildWorkspace.get_secret_refs(subject)
    assert clone.key == ("github", operator.id, "o/main", "")


@pytest.mark.asyncio
async def test_get_workspace_kwargs_carries_the_build_fields():
    sandbox = host_mod.Host(record=SimpleNamespace(id="h1", ssh_username="exedev"))  # type: ignore[arg-type]

    workflow = Build()
    workflow.input = Build._run_input_model()
    workflow.subject = SimpleNamespace(repo="o/app")
    workflow._profile = {"recommended_skills": ["python-house-rules"]}
    kwargs = await workflow.get_workspace_kwargs(sandbox)

    assert kwargs == {
        "host": sandbox,
        "subject": workflow.__dict__["subject"],
        "branch": None,
        "skills": ("python-house-rules",),
    }


def _pin_tracker(monkeypatch: pytest.MonkeyPatch, tracker: str) -> None:
    settings = SoftwareFactory.Settings(tracker=tracker)

    async def _settings(cls):
        return settings

    monkeypatch.setattr(SoftwareFactory, "settings", classmethod(_settings))


async def _required_servers(monkeypatch: pytest.MonkeyPatch, tracker: str):
    await connect_service(
        "github", identity={"app_id": "1", "slug": "druks-operator"}, secrets={"private_key": "pem"}
    )
    await connect_service(
        "github_reviewer",
        identity={"app_id": "2", "slug": "druks-reviewer"},
        secrets={"private_key": "reviewer-pem"},
    )
    monkeypatch.setattr(
        "druks.mcp.inbound.load_settings",
        lambda: SimpleNamespace(urls=SimpleNamespace(endpoint="https://druks.test")),
    )
    _pin_tracker(monkeypatch, tracker)
    return await BuildWorkspace.get_required_mcp_servers(SimpleNamespace(repo="o/main"))


async def test_a_board_build_requires_the_appliance_mcp(druks_db, monkeypatch):
    servers = await _required_servers(monkeypatch, "issues")

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


def _review_actor_stub(monkeypatch: pytest.MonkeyPatch, *, review_actor) -> None:
    async def _review_actor():
        return review_actor()

    monkeypatch.setattr("druks.contrib.software_factory.workflows.get_review_actor", _review_actor)


def test_the_build_clones_as_the_operator():
    assert BuildWorkspace.github is Github


@pytest.mark.asyncio
async def test_the_review_workspace_names_the_review_actors_identity(
    monkeypatch: pytest.MonkeyPatch, druks_db
):
    row = await connect_service(
        "github_reviewer",
        identity={"app_id": "2", "slug": "reviewer"},
        secrets={"private_key": "pem"},
    )
    _review_actor_stub(
        monkeypatch,
        review_actor=lambda: SimpleNamespace(service=GithubReviewer, client=None, mode="approve"),
    )

    [secret] = await ReviewWorkspace.get_secret_refs(SimpleNamespace(repo="o/app"))

    assert secret.key == ("github", row.id, "o/app", "")


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

    async def _client():
        return SimpleNamespace(get_bot_git_author=_bot_git_author)

    monkeypatch.setattr(workspace_mod, "get_github_client", _client)
    account = SimpleNamespace(username=username) if username else None

    async def _get_account(_id):
        return account

    monkeypatch.setattr(workspace_mod, "Account", SimpleNamespace(get=_get_account))


async def test_set_git_identity_stamps_the_workspace_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_path = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo_path)], check=True)
    workspace = RepoWorkspace(host=_IdentitySandbox(repo_path))  # type: ignore[arg-type]
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

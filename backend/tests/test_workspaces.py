import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from conftest import connect_service
from druks import workspaces as workspace_mod
from druks.contrib.software_factory.services import GithubReviewer
from druks.core.apis.github import GitHubAppClient
from druks.core.services import Github
from druks.harnesses.claude import ClaudeHarness
from druks.sandbox import SandboxSecret
from druks.sandbox.layout import get_repo_root
from druks.secrets.models import VaultSecret
from druks.workspaces import RepoWorkspace, Workspace

_CONFIG = SimpleNamespace(
    harness_class=ClaudeHarness,
    model="anthropic/claude-opus-4-8",
    fast_mode=False,
    effort=None,
    timeout=60,
    identity={},
    include_apps=False,
)


class _RecordingHost:
    id = "h1"
    ssh_username = "exedev"

    def __init__(self) -> None:
        self.events: list[tuple[Any, ...]] = []


async def test_every_agent_call_gets_the_workspace_env(druks_db):
    class DeployWorkspace(Workspace):
        def get_env(self) -> dict[str, str]:
            return {"DEPLOY_TOKEN": "token"}

    calls: list[dict[str, Any]] = []

    class _Host:
        async def run_agent(self, harness, manifest, **kwargs: Any) -> str:
            calls.append(kwargs)
            return "result"

    workspace = DeployWorkspace(None)
    workspace.host = _Host()  # type: ignore[assignment]
    await workspace.run_agent(
        harness=None,
        manifest=None,
        timeout=_CONFIG.timeout,
        identity=_CONFIG.identity,
        include_apps=_CONFIG.include_apps,
        prompt="p",
    )

    assert calls == [
        {
            "extra_env": {"DEPLOY_TOKEN": "token"},
            "prompt": "p",
            "timeout": 60,
            "identity": {},
            "include_apps": False,
        }
    ]


async def test_repo_workspace_prepares_the_clone_and_the_identity(
    monkeypatch: pytest.MonkeyPatch,
):
    host = _RecordingHost()
    subject = SimpleNamespace(repo="acme/widgets")
    workspace = RepoWorkspace(subject)
    workspace.branch = "feature"
    workspace.host = host  # type: ignore[assignment]

    async def _ensure(_host: Any, *, repo_url: str, ref: str | None, target_path: str) -> None:
        host.events.append(("clone", repo_url, ref, target_path))

    async def _git_identity(self: RepoWorkspace) -> None:
        host.events.append(("git_identity",))

    monkeypatch.setattr(workspace_mod.checkout, "ensure", _ensure)
    monkeypatch.setattr(RepoWorkspace, "set_git_identity", _git_identity)

    await workspace.prepare()
    assert host.events == [
        ("clone", "https://github.com/acme/widgets", "feature", get_repo_root("exedev")),
        ("git_identity",),
    ]


async def test_repo_workspace_diff_holds_commits_edits_and_untracked_files(tmp_path: Path):
    def git(*args: str, cwd: Path = tmp_path) -> str:
        identity = ("-c", "user.name=dev", "-c", "user.email=dev@example.com")
        return subprocess.run(
            ["git", *identity, *args], cwd=cwd, check=True, capture_output=True, text=True
        ).stdout

    git("init", "-q", "-b", "main", "origin")
    (tmp_path / "origin" / "kept.txt").write_text("kept\n")
    git("add", "kept.txt", cwd=tmp_path / "origin")
    git("commit", "-q", "-m", "start", cwd=tmp_path / "origin")
    git("clone", "-q", "origin", "checkout")
    checkout = tmp_path / "checkout"
    git("checkout", "-q", "-b", "feature", cwd=checkout)
    (checkout / "committed.txt").write_text("committed\n")
    git("add", "committed.txt", cwd=checkout)
    git("commit", "-q", "-m", "change", cwd=checkout)
    (checkout / "kept.txt").write_text("edited\n")
    (checkout / "untracked.txt").write_text("untracked\n")

    class _CheckoutHost:
        ssh_username = "exedev"

        async def exec(
            self, command: list[str], *, cwd: str = "", timeout: float = 30.0
        ) -> subprocess.CompletedProcess[str]:
            assert cwd == get_repo_root(self.ssh_username)
            return subprocess.run(command, cwd=checkout, capture_output=True, text=True)

    workspace = RepoWorkspace(None)
    workspace.host = _CheckoutHost()  # type: ignore[assignment]
    diff = await workspace.get_diff()

    for path in ("committed.txt", "kept.txt", "untracked.txt"):
        assert f"+++ b/{path}" in diff
    assert "?? untracked.txt" in git("status", "--short", cwd=checkout)


async def _connect(slug: str = "github") -> VaultSecret:
    return await connect_service(
        slug, identity={"app_id": "1", "slug": "druks-operator"}, secrets={"private_key": "pem"}
    )


async def test_repo_workspace_names_its_github_secret_and_repo_before_the_box_exists(druks_db):
    row = await _connect()
    subject = SimpleNamespace(repo="acme/widgets")

    [secret] = await RepoWorkspace.get_secrets(subject)

    assert secret == SandboxSecret(
        name="github", secret_id=row.id, resources=("acme/widgets",), host="github.com"
    )
    assert await Workspace.get_secrets(subject) == []


async def test_a_workspace_selects_its_github_identity_by_service(druks_db):
    row = await _connect("github_reviewer")

    class Reviewing(RepoWorkspace):
        github = GithubReviewer

    [secret] = await Reviewing.get_secrets(SimpleNamespace(repo="o/r"))

    assert (secret.secret_id, secret.resources) == (row.id, ("o/r",))


async def test_the_github_service_issues_its_installation_token(
    druks_db, monkeypatch: pytest.MonkeyPatch
):
    expiry = datetime.now(UTC) + timedelta(hours=1)
    asked: list[tuple] = []

    async def get_installation_id(self, owner_or_repo: str) -> int:
        return 7

    async def create_installation_token(self, installation_id, repositories=()):
        asked.append((self._app_id, installation_id, *repositories))
        return "ghs_operator", expiry

    monkeypatch.setattr(GitHubAppClient, "get_installation_id", get_installation_id)
    monkeypatch.setattr(GitHubAppClient, "create_installation_token", create_installation_token)
    await connect_service(
        "github",
        identity={"app_id": "1", "slug": "druks-operator"},
        secrets={"private_key": "operator-pem"},
    )

    assert await Github.issue_token(("acme/widgets",)) == ("ghs_operator", expiry)
    assert asked == [("1", 7, "widgets")]

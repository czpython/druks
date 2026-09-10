from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from conftest import connect_service
from druks import workspaces as workspace_mod
from druks.contrib.software_factory.services import GithubReviewer
from druks.core.apis.github import GitHubClient
from druks.core.services import Github
from druks.sandbox.layout import get_repo_root
from druks.secrets.models import VaultSecret
from druks.workspaces import RepoWorkspace, Workspace


class _RecordingHost:
    id = "h1"
    ssh_username = "exedev"

    def __init__(self) -> None:
        self.events: list[tuple[Any, ...]] = []


async def test_repo_workspace_clones_before_every_agent_call_and_writes_no_token(
    monkeypatch: pytest.MonkeyPatch,
):
    host = _RecordingHost()
    subject = SimpleNamespace(repo="acme/widgets")
    workspace = RepoWorkspace(host=host, subject=subject, branch="feature")  # type: ignore[arg-type]

    async def _ensure(_host: Any, *, repo_url: str, ref: str | None, target_path: str) -> None:
        host.events.append(("clone", repo_url, ref, target_path))

    async def _git_identity(self: RepoWorkspace, account_id: str | None) -> None:
        host.events.append(("git_identity", account_id))

    async def _run(self: Workspace, *, account_id: str | None, **kwargs: Any) -> str:
        host.events.append(("run_agent", account_id, kwargs))
        return "result"

    monkeypatch.setattr(workspace_mod.checkout, "ensure", _ensure)
    monkeypatch.setattr(RepoWorkspace, "set_git_identity", _git_identity)
    monkeypatch.setattr(Workspace, "run_agent", _run)

    assert await workspace.run_agent(account_id="acct", prompt="p") == "result"
    assert host.events == [
        ("clone", "https://github.com/acme/widgets", "feature", get_repo_root("exedev")),
        ("git_identity", "acct"),
        ("run_agent", "acct", {"prompt": "p"}),
    ]


async def _connect(slug: str = "github") -> VaultSecret:
    return await connect_service(
        slug, identity={"app_id": "1", "slug": "druks-operator"}, secrets={"private_key": "pem"}
    )


async def test_repo_workspace_names_its_github_secret_and_repo_before_the_box_exists(druks_db):
    row = await _connect()
    subject = SimpleNamespace(repo="acme/widgets")

    [secret] = await RepoWorkspace.get_secret_refs(subject)

    assert secret.key == ("github", row.id, "acme/widgets", "")
    assert await Workspace.get_secret_refs(subject) == []


async def test_a_workspace_selects_its_github_identity_by_service(druks_db):
    row = await _connect("github_reviewer")

    class Reviewing(RepoWorkspace):
        github = GithubReviewer

    [secret] = await Reviewing.get_secret_refs(SimpleNamespace(repo="o/r"))

    assert (secret.secret_id, secret.resource) == (row.id, "o/r")


async def test_the_github_service_issues_its_installation_token(
    druks_db, monkeypatch: pytest.MonkeyPatch
):
    expiry = datetime(2026, 9, 7, 18, 0, tzinfo=UTC)
    asked: list[tuple[str, str]] = []

    async def token_for_repo(self, repo: str) -> tuple[str, datetime]:
        asked.append((self._app_id, repo))
        return "ghs_operator", expiry

    monkeypatch.setattr(GitHubClient, "token_for_repo", token_for_repo)
    await connect_service(
        "github",
        identity={"app_id": "1", "slug": "druks-operator"},
        secrets={"private_key": "operator-pem"},
    )

    assert await Github.issue_token("acme/widgets") == ("ghs_operator", expiry)
    assert asked == [("1", "acme/widgets")]

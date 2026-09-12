from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from conftest import connect_service
from druks import workspaces as workspace_mod
from druks.accounts.models import Account, OperatorToken
from druks.contrib.software_factory.services import GithubReviewer
from druks.core.apis.github import GitHubClient
from druks.core.services import Github
from druks.durable.exceptions import FatalError
from druks.mcp.constants import THIS_APPLIANCE
from druks.mcp.exceptions import ReservedServerNameError
from druks.mcp.helpers import get_bearer_token_env_var
from druks.sandbox.datastructures import McpServer
from druks.sandbox.layout import get_repo_root
from druks.secrets.models import VaultSecret
from druks.workspaces import OperatorWrites, RepoWorkspace, Workspace, this_appliance_mcp_url


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


class _ApplianceHost:
    ssh_username = "exedev"

    def __init__(self, provider: str = "docker"):
        self.record = SimpleNamespace(provider=provider)
        self.run_agent = AsyncMock(return_value="ok")


def _endpoint(monkeypatch, endpoint: str):
    settings = MagicMock()
    settings.urls.endpoint = endpoint
    monkeypatch.setattr("druks.workspaces.load_settings", lambda: settings)
    return settings


def test_docker_sandbox_uses_host_docker_internal_for_loopback(monkeypatch):
    _endpoint(monkeypatch, "http://127.0.0.1:8001")

    assert (
        this_appliance_mcp_url(_ApplianceHost("docker")) == "http://host.docker.internal:8001/mcp"
    )
    assert this_appliance_mcp_url(_ApplianceHost("exe.dev")) == "http://127.0.0.1:8001/mcp"


def test_appliance_mcp_url_never_uses_webhook_host(monkeypatch):
    settings = _endpoint(monkeypatch, "https://druks.example.com")
    settings.urls.webhook_host = "hooks.example.com"

    assert this_appliance_mcp_url(_ApplianceHost("docker")) == "https://druks.example.com/mcp"


def test_appliance_mcp_url_refuses_an_unset_endpoint(monkeypatch):
    """Without the endpoint there is no address to hand the box, and a silent
    default would send it at a port nothing answers."""
    _endpoint(monkeypatch, "")

    with pytest.raises(FatalError, match="urls.endpoint"):
        this_appliance_mcp_url(_ApplianceHost("exe.dev"))


async def test_a_workspace_that_operates_the_appliance_injects_its_mcp(druks_db, monkeypatch):
    account = await Account.get_or_create("op@example.com")
    _endpoint(monkeypatch, "http://127.0.0.1:8001")
    workspace = Workspace(
        host=_ApplianceHost("docker"),  # type: ignore[arg-type]
        run_id="run-1",
        operator_writes=OperatorWrites.DENY,
    )

    kwargs = await workspace.with_mcp_servers(account.id, call_id="call-1")

    server = next(s for s in kwargs["mcp_servers"] if s.name == THIS_APPLIANCE)
    assert server.url == "http://host.docker.internal:8001/mcp"
    found = await OperatorToken.lookup(
        kwargs["extra_env"][get_bearer_token_env_var(THIS_APPLIANCE)]
    )
    assert found.account_id == account.id
    assert found.writes == OperatorWrites.DENY
    assert found.agent_call_id == "call-1"
    assert found.run_id == "run-1"


async def test_a_plain_workspace_gets_no_inward_credential(druks_db, monkeypatch):
    account = await Account.get_or_create("op@example.com")
    _endpoint(monkeypatch, "http://127.0.0.1:8001")

    kwargs = await Workspace(host=_ApplianceHost()).with_mcp_servers(  # type: ignore[arg-type]
        account.id, call_id="call-1"
    )

    assert THIS_APPLIANCE not in {server.name for server in kwargs.get("mcp_servers") or ()}
    assert "extra_env" not in kwargs


async def test_a_registry_server_cannot_claim_the_appliance_name(druks_db, monkeypatch):
    """One config key per name, so the collision is loud before the box rather
    than a server silently dropped from the emitted config."""
    account = await Account.get_or_create("op@example.com")
    _endpoint(monkeypatch, "http://127.0.0.1:8001")
    monkeypatch.setattr(
        Workspace,
        "get_mcp_delivery",
        AsyncMock(return_value=((McpServer(name=THIS_APPLIANCE, url="http://elsewhere"),), [])),
    )
    workspace = Workspace(
        host=_ApplianceHost(),  # type: ignore[arg-type]
        run_id="run-1",
        operator_writes=OperatorWrites.ALLOW,
    )

    with pytest.raises(ReservedServerNameError):
        await workspace.with_mcp_servers(account.id, call_id="call-1")


async def test_the_credential_is_gone_after_the_call(druks_db, monkeypatch):
    account = await Account.get_or_create("op@example.com")
    _endpoint(monkeypatch, "http://127.0.0.1:8001")
    host = _ApplianceHost("exe.dev")
    workspace = Workspace(
        host=host,  # type: ignore[arg-type]
        run_id="run-1",
        operator_writes=OperatorWrites.ALLOW,
    )

    await workspace.run_agent(account_id=account.id, call_id="call-done")

    token = host.run_agent.await_args.kwargs["extra_env"][get_bearer_token_env_var(THIS_APPLIANCE)]
    assert await OperatorToken.lookup(token) is None

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from druks.accounts.models import Account, OperatorToken
from druks.contrib.chat.enums import Autonomy
from druks.contrib.chat.models import Conversation
from druks.contrib.chat.workflows import Talk, TalkWorkspace
from druks.mcp.constants import THIS_APPLIANCE
from druks.mcp.helpers import get_bearer_token_env_var
from druks.workspaces import this_appliance_mcp_url


class _FakeHost:
    ssh_username = "exedev"

    def __init__(self, provider: str = "docker"):
        self.record = SimpleNamespace(provider=provider)
        self.run_agent = AsyncMock(return_value="ok")


def test_docker_sandbox_uses_host_docker_internal_for_loopback(monkeypatch):
    settings = MagicMock()
    settings.urls.endpoint = "http://127.0.0.1:8001"
    monkeypatch.setattr("druks.workspaces.load_settings", lambda: settings)

    assert this_appliance_mcp_url(_FakeHost("docker")) == "http://host.docker.internal:8001/mcp"
    assert this_appliance_mcp_url(_FakeHost("exe.dev")) == "http://127.0.0.1:8001/mcp"


def test_appliance_mcp_url_never_uses_webhook_host(monkeypatch):
    settings = MagicMock()
    settings.urls.endpoint = "https://druks.example.com"
    settings.urls.webhook_host = "hooks.example.com"
    monkeypatch.setattr("druks.workspaces.load_settings", lambda: settings)

    assert this_appliance_mcp_url(_FakeHost("docker")) == "https://druks.example.com/mcp"


async def test_talk_workspace_injects_this_appliance_mcp(druks_db, monkeypatch):
    account = await Account.get_or_create("op@example.com")
    settings = MagicMock()
    settings.urls.endpoint = "http://127.0.0.1:8001"
    monkeypatch.setattr("druks.workspaces.load_settings", lambda: settings)
    workspace = TalkWorkspace(
        host=_FakeHost("docker"),  # type: ignore[arg-type]
        account_id=account.id,
        run_id="run-1",
        writes="deny",
    )
    kwargs = await workspace.with_mcp_servers(account.id, call_id="call-1")
    server = next(s for s in kwargs["mcp_servers"] if s.name == THIS_APPLIANCE)
    token = kwargs["extra_env"][get_bearer_token_env_var(THIS_APPLIANCE)]

    assert server.url == "http://host.docker.internal:8001/mcp"
    found = await OperatorToken.lookup(token)
    assert found.account_id == account.id
    assert found.writes == "deny"
    assert found.agent_call_id == "call-1"


async def test_talk_workspace_revokes_the_token_after_the_call(druks_db, monkeypatch):
    account = await Account.get_or_create("op@example.com")
    settings = MagicMock()
    settings.urls.endpoint = "http://127.0.0.1:8001"
    monkeypatch.setattr("druks.workspaces.load_settings", lambda: settings)
    host = _FakeHost("exe.dev")
    workspace = TalkWorkspace(
        host=host,  # type: ignore[arg-type]
        account_id=account.id,
        run_id="run-1",
        writes="allow",
    )

    await workspace.run_agent(account_id=account.id, call_id="call-done")
    token = host.run_agent.await_args.kwargs["extra_env"][get_bearer_token_env_var(THIS_APPLIANCE)]
    assert await OperatorToken.lookup(token) is None


async def test_talk_reads_live_autonomy_for_the_next_call(druks_db):
    account = await Account.get_or_create("op@example.com")
    conversation = await Conversation.create(account_id=account.id, title="")
    flow = Talk()
    flow.subject = conversation
    flow.account_id = account.id
    flow._workflow_id = "run-1"

    kwargs = await flow.get_workspace_kwargs(_FakeHost())  # type: ignore[arg-type]
    assert kwargs["writes"] == "deny"

    conversation.autonomy = Autonomy.FULL
    kwargs = await flow.get_workspace_kwargs(_FakeHost())  # type: ignore[arg-type]
    assert kwargs["writes"] == "allow"

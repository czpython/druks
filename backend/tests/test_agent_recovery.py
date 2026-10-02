from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from conftest import connect_provider, make_agent_result
from drukbox_sdk.exceptions import SandboxNotFoundError, SandboxUnavailableError
from druks import agents
from druks.durable.engine import _step_engine
from druks.durable.models import AgentCall
from druks.harnesses.providers import AnthropicProvider
from druks.sandbox.client import Client
from druks.sandbox.exceptions import SandboxReleaseError
from druks.sandbox.models import SandboxIdentity, SecretRef
from druks.testing import seed_run
from druks.workflows import Workflow, current_workflow


class Answer(agents.AgentOutput):
    ok: bool


AGENT = agents.Agent(id="test.agent", prompt="test/prompt.md", contract=Answer, include_mcp=False)


@pytest.mark.parametrize("reuse", [False, True])
@pytest.mark.parametrize("already_deleted", [False, True])
async def test_recovery_deletes_the_orphan_before_preparing_a_new_workspace(
    druks_db, tmp_path, monkeypatch, reuse, already_deleted
):
    subscription = await connect_provider(
        AnthropicProvider, {"claudeAiOauth": {"accessToken": "test-token"}}
    )
    run = await seed_run(druks_db, kind="test", account_id=subscription.account_id)
    workflow = Workflow()
    workflow.account_id = subscription.account_id
    workflow._workflow_id = run.id
    workflow.steps_reuse_sandbox = reuse
    identity, _ = await SandboxIdentity.create(
        druks_db,
        account_id=subscription.account_id,
        run_id=run.id,
        scoped_to="workflow" if reuse else "test.agent",
        secret_refs=[SecretRef(name="anthropic", secret_id=subscription.id)],
    )
    await identity.bind("abandoned-host")
    await AgentCall.start(
        _step_engine(),
        call_id="abandoned-call",
        run_id=run.id,
        model="anthropic/claude-opus-4-7",
        agent="test.agent",
        host_id=identity.host_id,
        subscription_id=subscription.id,
        api_key_id=None,
    )
    if reuse:
        workflow._host = SimpleNamespace(id=identity.host_id)
    host = MagicMock(id="replacement-host")
    host.run_agent = AsyncMock(return_value=make_agent_result({"ok": True}))
    prepared = []

    async def provision(self, **kwargs):
        prepared.append(host.id)
        await druks_db.refresh(identity)
        assert identity.revoked_at
        assert delete.await_count == 2
        await kwargs["identity"].bind(host.id)
        return host

    @asynccontextmanager
    async def ephemeral(self, **kwargs):
        yield await provision(self, **kwargs)

    @asynccontextmanager
    async def attach(self, *, host_id):
        assert host_id == host.id
        yield host

    delete = AsyncMock(
        side_effect=[
            SandboxUnavailableError("delete failed"),
            SandboxNotFoundError("gone") if already_deleted else None,
        ]
    )
    api = SimpleNamespace(delete_host=delete, renew_host=AsyncMock(), aclose=AsyncMock())
    monkeypatch.setattr(Client, "_api", lambda self: api)
    monkeypatch.setattr(Client, "provision", provision)
    monkeypatch.setattr(Client, "ephemeral", ephemeral)
    monkeypatch.setattr(Client, "attach", attach)
    monkeypatch.setattr(agents, "load_settings", lambda: SimpleNamespace(artifacts_dir=tmp_path))
    monkeypatch.setattr(agents, "render_prompt", AsyncMock(return_value="Read the repository."))
    token = current_workflow.set(workflow)
    try:
        with pytest.raises(SandboxReleaseError, match="abandoned-host"):
            await AGENT._run(druks_db, workflow_id=run.id)
        assert not prepared
        host.run_agent.assert_not_awaited()
        orphan = await druks_db.get(AgentCall, "abandoned-call")
        assert orphan.status == "running"

        assert await AGENT._run(druks_db, workflow_id=run.id) == Answer(ok=True)
    finally:
        current_workflow.reset(token)

    assert prepared == ["replacement-host"]
    assert [call.args for call in delete.await_args_list] == [("abandoned-host",)] * 2
    await druks_db.refresh(orphan)
    assert orphan.status == "abandoned"
    assert orphan.finished_at
    calls = await AgentCall.list_for_run(druks_db, run.id)
    assert {call.sandbox_host_id: call.status for call in calls} == {
        "abandoned-host": "abandoned",
        "replacement-host": "succeeded",
    }

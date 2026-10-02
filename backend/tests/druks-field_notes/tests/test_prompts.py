from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from conftest import connect_provider, make_agent_result
from druks import agents
from druks.harnesses.providers import AnthropicProvider
from druks.prompts import resolver
from druks.testing import seed_run
from druks.workflows import WorkflowError, current_workflow
from druks.workspaces import RepoWorkspace, Workspace
from druks_field_notes.app import FieldNotes
from druks_field_notes.models import Repository
from druks_field_notes.workflows import Survey
from jinja2 import DictLoader
from jinja2.sandbox import ImmutableSandboxedEnvironment


@pytest.mark.parametrize(
    ("agent", "expected", "paths"),
    [
        (FieldNotes.summarize, "Bundled unrelated/data", []),
        (
            FieldNotes.survey,
            "Remote unrelated/data",
            [
                ("trusted/widgets", ".druks/field_notes/prompts/survey.md"),
                ("trusted/.druks", "field_notes/prompts/survey.md"),
            ],
        ),
    ],
)
async def test_prompt_override_uses_the_declared_workspace_repository(
    druks_db, tmp_path, monkeypatch, agent, expected, paths
):
    subscription = await connect_provider(
        AnthropicProvider, {"claudeAiOauth": {"accessToken": "test-token"}}
    )
    repository = await Repository.create(repo="trusted/widgets")
    run = await seed_run(druks_db, kind=Survey.kind, subject=repository)
    workflow = Survey()
    workflow.subject = repository
    workflow.account_id = subscription.account_id
    workflow._workflow_id = run.id
    runner = SimpleNamespace(
        host_id="sandbox",
        prepare_context=AsyncMock(side_effect=lambda session, context, **kwargs: context),
        run_agent=AsyncMock(return_value=make_agent_result({"gist": "A widget library."})),
    )

    @asynccontextmanager
    async def sandbox(*args):
        yield runner

    monkeypatch.setattr(agents, "_runner", sandbox)
    monkeypatch.setattr(agents, "load_settings", lambda: SimpleNamespace(artifacts_dir=tmp_path))
    monkeypatch.setattr(workflow, "_lease_host", AsyncMock(return_value=None))
    monkeypatch.setattr(RepoWorkspace, "get_secrets", AsyncMock(return_value=[]))
    monkeypatch.setattr(RepoWorkspace, "get_all_mcp_servers", AsyncMock(return_value=((), [])))
    environment = ImmutableSandboxedEnvironment(
        loader=DictLoader({agent.prompt: "Bundled {{ repo }}"}), enable_async=True
    )
    monkeypatch.setattr(resolver, "_environment", lambda: environment)
    fetch = AsyncMock(side_effect=[None, "Remote {{ repo }}"])
    monkeypatch.setattr(resolver, "fetch_file", fetch)
    token = current_workflow.set(workflow)
    try:
        await agent._run(druks_db, workflow_id=run.id, repo="unrelated/data")
    finally:
        current_workflow.reset(token)

    assert runner.run_agent.await_args.kwargs["prompt"] == expected
    assert [(call.kwargs["repo"], call.kwargs["path"]) for call in fetch.await_args_list] == paths

    workflow.workspace_class = Workspace
    token = current_workflow.set(workflow)
    try:
        with pytest.raises(WorkflowError, match="does not use RepoWorkspace"):
            await FieldNotes.survey._run(druks_db, workflow_id=run.id)
    finally:
        current_workflow.reset(token)

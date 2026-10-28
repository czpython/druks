import subprocess
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

from conftest import connect_service
from druks.sandbox.layout import get_repo_root
from druks.testing import seed_run
from druks.workflows import Workflow
from druks.workspaces import RepoWorkspace
from druks_field_notes.models import Repository
from druks_field_notes.workflows import Survey


async def test_exec_runs_the_command_in_a_box_with_the_repo_cloned(druks_db, monkeypatch):
    await connect_service(
        "github", identity={"app_id": "1", "slug": "druks-operator"}, secrets={"private_key": "pem"}
    )
    box = SimpleNamespace(
        id="host-1",
        ssh_username="exedev",
        exec=AsyncMock(return_value=subprocess.CompletedProcess([], 1, "out", "")),
    )
    boxes = []

    @asynccontextmanager
    async def ephemeral(self, *, idempotency_key=None, secrets=None, **_kwargs):
        boxes.append(secrets)
        yield box

    monkeypatch.setattr("druks.sandbox.client.Client.ephemeral", ephemeral)
    clone = AsyncMock()
    monkeypatch.setattr("druks.workspaces.checkout.ensure", clone)
    run = await seed_run(druks_db, kind=Survey.kind, run_id="wf-9")
    workflow = Workflow()
    workflow.workspace_class = RepoWorkspace
    workflow.subject = await Repository.create(repo="acme/widgets")
    workflow.account_id = run.account_id
    workflow._workflow_id = run.id

    result = await workflow.exec(["druks-house-rules", "check"], timeout=60)

    assert result.returncode == 1
    assert result.stdout == "out"
    [secrets] = boxes
    assert "github" in secrets
    clone.assert_awaited_once_with(
        box,
        repo_url="https://github.com/acme/widgets",
        ref=None,
        target_path=get_repo_root("exedev"),
    )
    box.exec.assert_awaited_once_with(
        ["druks-house-rules", "check"], cwd=get_repo_root("exedev"), timeout=60
    )

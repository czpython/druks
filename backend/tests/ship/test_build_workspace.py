from types import SimpleNamespace
from typing import Any

import pytest
from druks.contrib.ship.constants import GITHUB_MCP_NAME, GITHUB_MCP_URL
from druks.contrib.ship.workflows import Build, BuildWorkspace
from druks.mcp.helpers import get_bearer_token_env_var
from druks.sandbox import host as host_mod
from druks.sandbox.layout import get_related_root
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

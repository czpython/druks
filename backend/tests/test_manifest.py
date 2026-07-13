import json
from unittest import mock

from conftest import make_settings, seed_agent_run
from druks.durable.schemas import AgentCallFiles
from druks.harnesses.artifacts import persist_manifest
from druks.harnesses.base import Harness
from druks.harnesses.claude import ClaudeHarness
from druks.mcp import models
from druks.mcp.helpers import get_bearer_token_env_var
from druks.sandbox.datastructures import McpServer
from druks.skills.datastructures import InstalledSkill
from druks.skills.models import Skill, SkillCollection

_LINEAR_URL = "https://mcp.linear.app/mcp"
_LINEAR_ENV = get_bearer_token_env_var("linear")
_TOKEN = "lin_secret_value"


def _build(
    *,
    harness: Harness | None = None,
    mcp_servers: tuple[McpServer, ...] = (),
    extra_env: dict[str, str] | None = None,
) -> dict:
    # get_manifest never touches the live sandbox, so the harness builds
    # without sandbox settings — the same shape argv unit tests use.
    harness = harness or ClaudeHarness(model="claude-opus-4-8", fast_mode=False, effort=None)
    return harness.get_manifest(mcp_servers=mcp_servers, extra_env=extra_env)


def _seed_skills(*names: str, disabled: tuple[str, ...] = ()) -> None:
    collection = SkillCollection.create(
        source="test",
        name="test skills",
        skills=[
            InstalledSkill(name=name, description=f"{name} skill", path=name, content_hash="x")
            for name in names
        ],
    )
    for skill in collection.skills:
        if skill.name in disabled:
            skill.enabled = False


# --- the recorded capability set ------------------------------------------


def test_manifest_records_the_delivered_capability_set(db_session):
    """Records model, harness, each MCP server's declared/delivered/token
    presence, and the enabled skills."""
    models.McpServer.create(name="linear", url=_LINEAR_URL, token=_TOKEN)
    _seed_skills("alpha", "beta")

    linear = McpServer(name="linear", url=_LINEAR_URL, bearer_token_env_var=_LINEAR_ENV)
    github = McpServer(
        name="github",
        url="https://api.githubcopilot.com/mcp/",
        bearer_token_env_var=get_bearer_token_env_var("github"),
    )
    # Both servers delivered with their token — github is build's own
    # requirement (get_required_mcp_servers), so it reads delivered but not declared.
    manifest = _build(
        mcp_servers=(linear, github),
        extra_env={
            _LINEAR_ENV: _TOKEN,
            get_bearer_token_env_var("github"): "ghs_from_build",
        },
    )

    assert manifest["model"] == "claude-opus-4-8"
    assert manifest["harness"] == "claude"
    assert manifest["skills_enabled"] == ["alpha", "beta"]

    linear_entry = next(s for s in manifest["mcp_servers"] if s["name"] == "linear")
    assert linear_entry["declared"] is True
    assert linear_entry["delivered"] is True
    assert linear_entry["token_present"] is True

    github_entry = next(s for s in manifest["mcp_servers"] if s["name"] == "github")
    assert github_entry["declared"] is False
    assert github_entry["delivered"] is True
    assert github_entry["token_present"] is True


def test_missing_mcp_token_records_absence(db_session):
    """A delivered server whose bearer var is absent from the run env reads
    token_present False — recorded, not failed."""
    models.McpServer.create(name="linear", url=_LINEAR_URL, token=_TOKEN)

    manifest = _build(
        mcp_servers=(McpServer(name="linear", url=_LINEAR_URL, bearer_token_env_var=_LINEAR_ENV),),
        extra_env={},
    )

    linear_entry = next(s for s in manifest["mcp_servers"] if s["name"] == "linear")
    assert linear_entry["declared"] is True
    assert linear_entry["token_present"] is False


def test_declared_but_undelivered_server_still_reads_declared(db_session):
    """An enabled registry server is declared even on a call that didn't
    deliver it — declared True, delivered False, so the manifest shows exactly
    what this call ran without."""
    models.McpServer.create(name="linear", url=_LINEAR_URL, token=_TOKEN)

    manifest = _build()

    linear_entry = next(s for s in manifest["mcp_servers"] if s["name"] == "linear")
    assert linear_entry["declared"] is True
    assert linear_entry["delivered"] is False
    assert linear_entry["token_present"] is False


def test_records_the_delivered_server_not_the_registry_duplicate(db_session):
    """When a workspace requires a server under an enabled entry's name, the
    workspace's wins delivery — the manifest records what the harness actually
    ran (the delivered url/env var), not the registry's values."""
    models.McpServer.create(name="linear", url=_LINEAR_URL, token=_TOKEN)
    required = McpServer(
        name="linear",
        url="https://required.internal/linear",
        bearer_token_env_var="REQUIRED_LINEAR_TOKEN",
    )

    manifest = _build(mcp_servers=(required,), extra_env={"REQUIRED_LINEAR_TOKEN": "s"})

    linear_entry = next(s for s in manifest["mcp_servers"] if s["name"] == "linear")
    assert linear_entry["url"] == "https://required.internal/linear"
    assert linear_entry["bearer_token_env_var"] == "REQUIRED_LINEAR_TOKEN"
    assert linear_entry["declared"] is True
    assert linear_entry["delivered"] is True
    assert linear_entry["token_present"] is True


# --- hash: identical capabilities bucket together --------------------------


def test_hash_is_stable_for_identical_capabilities(db_session):
    """Identical capability sets hash the same; changing the model, MCP token
    availability, or the enabled skill set moves the hash."""
    models.McpServer.create(name="linear", url=_LINEAR_URL, token=_TOKEN)
    linear = McpServer(name="linear", url=_LINEAR_URL, bearer_token_env_var=_LINEAR_ENV)
    with_token = {"mcp_servers": (linear,), "extra_env": {_LINEAR_ENV: _TOKEN}}

    baseline = _build(**with_token)
    assert _build(**with_token)["manifest_hash"] == baseline["manifest_hash"]

    different_model = _build(
        harness=ClaudeHarness(model="claude-sonnet-5", fast_mode=False, effort=None),
        **with_token,
    )
    assert different_model["manifest_hash"] != baseline["manifest_hash"]

    without_token = _build(mcp_servers=(linear,), extra_env={})
    assert without_token["manifest_hash"] != baseline["manifest_hash"]


def test_disabling_a_skill_changes_the_recorded_set_and_the_hash(db_session):
    """The skills tar excludes disabled skills, so the enabled set is the call's
    real skill capability: recorded and hashed, so two calls with different
    enabled skills bucket apart."""
    _seed_skills("alpha", "beta")
    both = _build()
    assert both["skills_enabled"] == ["alpha", "beta"]

    Skill.get("beta").enabled = False
    db_session.flush()
    one = _build()

    assert one["skills_enabled"] == ["alpha"]
    assert one["manifest_hash"] != both["manifest_hash"]


# --- presence, never the value -------------------------------------------


def test_manifest_records_token_presence_never_the_value(db_session):
    """The secret token never lands in the manifest — only its env-var name and
    a presence boolean."""
    models.McpServer.create(name="linear", url=_LINEAR_URL, token=_TOKEN)
    linear = McpServer(name="linear", url=_LINEAR_URL, bearer_token_env_var=_LINEAR_ENV)

    manifest = _build(mcp_servers=(linear,), extra_env={_LINEAR_ENV: _TOKEN})

    serialized = json.dumps(manifest)
    assert _TOKEN not in serialized
    assert _LINEAR_ENV in serialized  # the var name is safe to record


def test_manifest_stays_presence_only_for_a_declared_header_server(db_session):
    """A server delivered with declared headers records the same presence-only
    entry: no header value — plain or secret — lands in the manifest, and a
    bearer-less server simply reads token_present False."""
    models.McpServer.create(
        name="grafana",
        url="https://mcp.grafana.com/mcp",
        token_source="",
        headers={"X-Grafana-URL": "https://acme.grafana.net"},
        secret_headers={"X-Api-Key": "grafana-api-secret"},
    )
    delivered = McpServer(
        name="grafana",
        url="https://mcp.grafana.com/mcp",
        headers={"X-Grafana-URL": "https://acme.grafana.net"},
        env_headers={"X-Api-Key": "MCP_GRAFANA_HEADER_X_API_KEY"},
    )

    manifest = _build(
        mcp_servers=(delivered,),
        extra_env={"MCP_GRAFANA_HEADER_X_API_KEY": "grafana-api-secret"},
    )

    serialized = json.dumps(manifest)
    assert "grafana-api-secret" not in serialized
    assert "acme.grafana.net" not in serialized
    grafana_entry = next(s for s in manifest["mcp_servers"] if s["name"] == "grafana")
    assert grafana_entry["declared"] is True
    assert grafana_entry["delivered"] is True
    assert grafana_entry["token_present"] is False


# --- persistence + surfacing in transcript files -------------------------


def test_persist_writes_manifest_into_the_call_dir(tmp_path, db_session):
    manifest = _build()

    path = persist_manifest(tmp_path, call_id="call-1", manifest=manifest)

    assert path == tmp_path / "call-1" / "manifest.json"
    assert json.loads(path.read_text())["manifest_hash"] == manifest["manifest_hash"]


def test_manifest_surfaces_in_agent_call_files(tmp_path, db_session):
    """A written manifest.json is inventoried on the call's transcript files, in
    the manifest slot under its downloadable file name."""
    call = seed_agent_run(agent="implement")
    manifest = _build()
    with mock.patch("druks.durable.models.load_settings", return_value=make_settings(tmp_path)):
        persist_manifest(call.call_dir.parent, call_id=call.call_dir.name, manifest=manifest)
        files = AgentCallFiles.from_call(call, None)

    assert files.manifest
    assert files.manifest.name == "manifest.json"
    assert files.manifest.size_bytes > 0

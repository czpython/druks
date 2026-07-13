from pathlib import Path

import httpx
from conftest import configure_app_for_test, make_settings
from druks.harnesses import base as hbase
from fastapi.testclient import TestClient


def _build_client(tmp_path: Path) -> TestClient:
    return TestClient(configure_app_for_test(settings=make_settings(tmp_path)))


def test_get_settings_returns_default_utc_when_no_row_exists(tmp_path: Path):
    with _build_client(tmp_path) as client:
        response = client.get("/api/settings")

    assert response.status_code == 200
    body = response.json()
    assert body["timezone"] == "UTC"
    assert "updatedAt" in body


def test_get_harnesses_lists_seeded_defaults(tmp_path: Path):
    with _build_client(tmp_path) as client:
        harnesses = {h["name"]: h for h in client.get("/api/settings/harnesses").json()}
    assert harnesses["claude"]["provider"] == "anthropic"
    assert harnesses["claude"]["model"] == "claude-opus-4-7"
    assert "claude-sonnet-4-6" in harnesses["claude"]["allowedModels"]
    assert harnesses["codex"]["provider"] == "openai"
    assert (harnesses["codex"]["effort"], harnesses["codex"]["timeout"]) == ("high", 1800)


def test_harness_response_carries_connection_state(tmp_path: Path):
    with _build_client(tmp_path) as client:
        claude = {h["name"]: h for h in client.get("/api/settings/harnesses").json()}["claude"]
    assert claude["connected"] is False
    assert claude["account"] is None
    assert "expiresAt" in claude


def test_login_start_returns_authorize_url(tmp_path: Path):
    with _build_client(tmp_path) as client:
        response = client.post("/api/settings/harnesses/claude/login/start")
    assert response.status_code == 200
    assert response.json()["authorizeUrl"].startswith("https://claude.ai/oauth/authorize?")


def test_login_start_unknown_harness_is_404(tmp_path: Path):
    with _build_client(tmp_path) as client:
        response = client.post("/api/settings/harnesses/grok/login/start")
    assert response.status_code == 404


def test_login_complete_with_no_code_is_422(tmp_path: Path):
    with _build_client(tmp_path) as client:
        client.post("/api/settings/harnesses/claude/login/start")
        # Empty paste fails before any provider call — clean check of the
        # LoginError -> 422 wiring without mocking the exchange.
        response = client.post("/api/settings/harnesses/claude/login/complete", json={"code": ""})
    assert response.status_code == 422


def test_login_complete_provider_rejection_is_422(tmp_path: Path, monkeypatch):
    async def fake_post(self, url, *, json=None, data=None, **kwargs):
        return httpx.Response(
            400,
            text="invalid_grant: code expired",
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(hbase.httpx.AsyncClient, "post", fake_post)
    with _build_client(tmp_path) as client:
        client.post("/api/settings/harnesses/claude/login/start")
        response = client.post(
            "/api/settings/harnesses/claude/login/complete", json={"code": "code"}
        )

    assert response.status_code == 422
    assert "invalid_grant" in response.json()["detail"]


def test_disconnect_returns_disconnected_status(tmp_path: Path):
    with _build_client(tmp_path) as client:
        response = client.delete("/api/settings/harnesses/claude/login")
    assert response.status_code == 200
    assert response.json()["connected"] is False


def test_patch_settings_persists_valid_iana_zone(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("druks.user_settings.routes.apply_schedules", lambda: None)
    with _build_client(tmp_path) as client:
        patch = client.patch("/api/settings", json={"timezone": "Europe/Madrid"})
        assert patch.status_code == 200
        assert patch.json()["timezone"] == "Europe/Madrid"

        get = client.get("/api/settings")
        assert get.status_code == 200
        assert get.json()["timezone"] == "Europe/Madrid"


def test_patch_settings_rejects_invalid_timezone(tmp_path: Path):
    with _build_client(tmp_path) as client:
        response = client.patch("/api/settings", json={"timezone": "Not/A/Zone"})

    assert response.status_code == 422
    body = response.json()
    assert "Not/A/Zone" in body["detail"]


def test_timezone_change_reconciles_schedules(tmp_path: Path, monkeypatch):
    """Crons are evaluated in the operator's timezone, so changing it repoints
    the DBOS schedules now; re-asserting the same zone doesn't churn them."""
    reconciled = []
    monkeypatch.setattr(
        "druks.user_settings.routes.apply_schedules", lambda: reconciled.append(True)
    )
    with _build_client(tmp_path) as client:
        patch = client.patch("/api/settings", json={"timezone": "Europe/Madrid"})
        assert patch.status_code == 200
        assert len(reconciled) == 1

        patch = client.patch("/api/settings", json={"timezone": "Europe/Madrid"})
        assert patch.status_code == 200
        assert len(reconciled) == 1


def test_patch_harness_updates_model_and_fast_mode(tmp_path: Path):
    with _build_client(tmp_path) as client:
        patch = client.patch(
            "/api/settings/harnesses/claude",
            json={"model": "claude-sonnet-4-6", "fastMode": True},
        )
        assert patch.status_code == 200
        body = patch.json()
        assert body["model"] == "claude-sonnet-4-6"
        assert body["fastMode"] is True
        listed = {h["name"]: h for h in client.get("/api/settings/harnesses").json()}
        assert listed["claude"]["model"] == "claude-sonnet-4-6"
        # The other harness is untouched.
        assert listed["codex"]["model"] == "gpt-5.5"


def test_patch_harness_rejects_model_from_another_harness(tmp_path: Path):
    with _build_client(tmp_path) as client:
        # gpt-5.5 belongs to codex, not claude.
        response = client.patch("/api/settings/harnesses/claude", json={"model": "gpt-5.5"})
    assert response.status_code == 422
    assert "gpt-5.5" in response.json()["detail"]


def test_patch_unknown_harness_is_404(tmp_path: Path):
    with _build_client(tmp_path) as client:
        response = client.patch("/api/settings/harnesses/grok", json={"effort": "low"})
    assert response.status_code == 404


def _build_extension(client: TestClient) -> dict:
    body = client.get("/api/settings/extensions").json()
    return next(m for m in body["extensions"] if m["name"] == "build")


def test_extensions_surface_build_agents(tmp_path: Path):
    """The build pipeline's agents all tune under the build extension."""
    with _build_client(tmp_path) as client:
        body = client.get("/api/settings/extensions").json()
    extensions = {m["name"]: m for m in body["extensions"]}

    build_agents = {a["name"]: a for a in extensions["build"]["agents"]}
    # The build pipeline's plan stage stays; the standalone Plan-tab agent is gone.
    assert "generate_plan" in build_agents
    assert "planning" not in build_agents


def test_extensions_surface_build_agents_and_workflow_defaults(tmp_path: Path):
    with _build_client(tmp_path) as client:
        build = _build_extension(client)

    agents = {a["name"]: a for a in build["agents"]}
    # An agent's family-token default resolves to the family's model; effort
    # and timeout inherit the global defaults ("high", 1800s) when the agent
    # declares neither and the operator set no override.
    assert agents["generate_plan"] == {
        "name": "generate_plan",
        "description": "brief → implementation plan",
        "model": "gpt-5.5",
        "source": "default",
        "default": "codex",
        "effort": "high",
        "effortSource": "harness",
        "timeout": 1800,
        "timeoutSource": "harness",
    }
    assert agents["implement"]["model"] == "claude-opus-4-7"
    assert agents["implement"]["default"] == "claude"
    # evaluate declares medium effort; the rest inherit the global default.
    assert agents["evaluate_implementation"]["effort"] == "medium"
    assert agents["evaluate_implementation"]["effortSource"] == "declared"
    # The workflow's settings surface alongside its agents.
    fields = {f["name"]: f for f in build["workflows"][0]["fields"]}
    assert fields["max_implementation_revisions"]["value"] == 5
    assert fields["auto_dispatch_on_plan_approval"]["value"] is False


def test_extensions_override_agent_model_persists(tmp_path: Path):
    with _build_client(tmp_path) as client:
        patch = client.patch(
            "/api/settings/extensions",
            json={"agentModels": {"implement": "gpt-5.5"}},
        )
        assert patch.status_code == 200
        agents = {a["name"]: a for a in _build_extension(client)["agents"]}

    assert agents["implement"]["model"] == "gpt-5.5"
    assert agents["implement"]["source"] == "agent"


def test_extensions_harness_effort_and_per_agent_effort_override(tmp_path: Path):
    with _build_client(tmp_path) as client:
        agents = {a["name"]: a for a in _build_extension(client)["agents"]}
        # generate_plan runs on codex and inherits the codex harness effort.
        assert agents["generate_plan"]["effort"] == "high"
        assert agents["generate_plan"]["effortSource"] == "harness"

        # Retune the codex harness effort + override one agent.
        client.patch("/api/settings/harnesses/codex", json={"effort": "low"})
        client.patch("/api/settings/extensions", json={"agentEfforts": {"generate_plan": "high"}})
        agents = {a["name"]: a for a in _build_extension(client)["agents"]}
        # generate_plan overridden; revise_contract (also codex) inherits "low".
        assert agents["generate_plan"]["effort"] == "high"
        assert agents["generate_plan"]["effortSource"] == "agent"
        assert agents["revise_contract"]["effort"] == "low"
        assert agents["revise_contract"]["effortSource"] == "harness"


def test_extensions_reject_unknown_effort(tmp_path: Path):
    with _build_client(tmp_path) as client:
        response = client.patch(
            "/api/settings/extensions",
            json={"agentEfforts": {"implement": "turbo"}},
        )
    assert response.status_code == 422
    assert "turbo" in response.json()["detail"]


def test_extensions_harness_timeout_and_per_agent_timeout_override(tmp_path: Path):
    with _build_client(tmp_path) as client:
        agents = {a["name"]: a for a in _build_extension(client)["agents"]}
        # implement runs on claude and inherits the claude harness timeout.
        assert agents["implement"]["timeout"] == 1800
        assert agents["implement"]["timeoutSource"] == "harness"

        # Retune the claude harness timeout + override one agent.
        client.patch("/api/settings/harnesses/claude", json={"timeout": 1200})
        client.patch("/api/settings/extensions", json={"agentTimeouts": {"implement": 3600}})
        agents = {a["name"]: a for a in _build_extension(client)["agents"]}
        # implement overridden; review_plan (also claude) inherits 1200.
        assert agents["implement"]["timeout"] == 3600
        assert agents["implement"]["timeoutSource"] == "agent"
        assert agents["review_plan"]["timeout"] == 1200
        assert agents["review_plan"]["timeoutSource"] == "harness"


def test_extensions_reject_non_positive_timeout(tmp_path: Path):
    with _build_client(tmp_path) as client:
        response = client.patch(
            "/api/settings/extensions",
            json={"agentTimeouts": {"implement": 0}},
        )
    assert response.status_code == 422


def test_build_review_code_is_a_workflow_setting(tmp_path: Path):
    """Gating the code reviewer is a build-workflow boolean, not an agent flag."""
    with _build_client(tmp_path) as client:
        workflow = _build_extension(client)["workflows"][0]
        fields = {f["name"]: f for f in workflow["fields"]}
        assert fields["review_code"]["value"] is True
        assert fields["review_code"]["overridden"] is False

        patch = client.patch(
            "/api/settings/extensions",
            json={"workflowSettings": {workflow["kind"]: {"review_code": False}}},
        )
        assert patch.status_code == 200
        fields = {f["name"]: f for f in _build_extension(client)["workflows"][0]["fields"]}
        assert fields["review_code"]["value"] is False
        assert fields["review_code"]["overridden"] is True


def _extension(client: TestClient, name: str) -> dict:
    body = client.get("/api/settings/extensions").json()
    return next(m for m in body["extensions"] if m["name"] == name)


def _poll_usage_fields(client: TestClient) -> dict:
    workflows = {w["kind"]: w for w in _extension(client, "usage")["workflows"]}
    return {f["name"]: f for f in workflows["usage.poll_usage"]["fields"]}


def test_scheduled_workflow_surfaces_schedule_fields(tmp_path: Path):
    """A workflow's every= surfaces as two ordinary settings fields on the
    extension that owns it."""
    with _build_client(tmp_path) as client:
        fields = _poll_usage_fields(client)
        assert fields["schedule"]["value"] == "*/5 * * * *"
        assert fields["schedule"]["default"] == "*/5 * * * *"
        assert fields["schedule"]["overridden"] is False
        assert fields["schedule_enabled"]["value"] is True
        assert fields["schedule_enabled"]["type"] == "bool"


def test_schedule_override_persists_and_reconciles(tmp_path: Path, monkeypatch):
    """Overriding the cadence or pausing persists like any workflow setting and
    repoints the DBOS crons now, not at the next launch."""
    reconciled = []
    monkeypatch.setattr(
        "druks.user_settings.routes.apply_schedules", lambda: reconciled.append(True)
    )
    with _build_client(tmp_path) as client:
        patch = client.patch(
            "/api/settings/extensions",
            json={
                "workflowSettings": {
                    "usage.poll_usage": {
                        "schedule": "0 9 * * *",
                        "schedule_enabled": False,
                    }
                }
            },
        )
        assert patch.status_code == 200
        assert reconciled
        fields = _poll_usage_fields(client)
        assert fields["schedule"]["value"] == "0 9 * * *"
        assert fields["schedule"]["overridden"] is True
        assert fields["schedule_enabled"]["value"] is False


def test_schedule_rejects_invalid_cron(tmp_path: Path):
    # A malformed cron would be silently never-fired by DBOS — reject at the write.
    with _build_client(tmp_path) as client:
        patch = client.patch(
            "/api/settings/extensions",
            json={"workflowSettings": {"usage.poll_usage": {"schedule": "not a cron"}}},
        )
        assert patch.status_code == 422


def test_extensions_clearing_an_override_reverts_to_the_family_default(tmp_path: Path):
    with _build_client(tmp_path) as client:
        client.patch(
            "/api/settings/extensions", json={"agentModels": {"generate_plan": "claude-opus-4-7"}}
        )
        agents = {a["name"]: a for a in _build_extension(client)["agents"]}
        assert agents["generate_plan"]["model"] == "claude-opus-4-7"
        assert agents["generate_plan"]["source"] == "agent"

        # Null clears the override; the agent falls back to its family default.
        client.patch("/api/settings/extensions", json={"agentModels": {"generate_plan": None}})
        agents = {a["name"]: a for a in _build_extension(client)["agents"]}
        assert agents["generate_plan"]["model"] == "gpt-5.5"
        assert agents["generate_plan"]["source"] == "default"


def test_extensions_reject_unknown_agent_model(tmp_path: Path):
    with _build_client(tmp_path) as client:
        # No installed harness owns this namespace, so nothing could run it.
        response = client.patch(
            "/api/settings/extensions",
            json={"agentModels": {"implement": "llama-3-70b"}},
        )
    assert response.status_code == 422
    assert "llama-3-70b" in response.json()["detail"]


def test_extensions_override_workflow_setting_persists(tmp_path: Path):
    with _build_client(tmp_path) as client:
        patch = client.patch(
            "/api/settings/extensions",
            json={
                "workflowSettings": {"build.build_workflow": {"max_implementation_revisions": 8}}
            },
        )
        assert patch.status_code == 200
        fields = {f["name"]: f for f in _build_extension(client)["workflows"][0]["fields"]}

    assert fields["max_implementation_revisions"]["value"] == 8
    assert fields["max_implementation_revisions"]["overridden"] is True


def test_extensions_reject_out_of_range_workflow_setting(tmp_path: Path):
    with _build_client(tmp_path) as client:
        response = client.patch(
            "/api/settings/extensions",
            json={"workflowSettings": {"build_workflow": {"max_implementation_revisions": 99}}},
        )
    assert response.status_code == 422

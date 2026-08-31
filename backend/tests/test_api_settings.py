from pathlib import Path

from druks.contrib.review.app import Review
from druks.contrib.software_factory.app import SoftwareFactory
from druks.database import db_session
from druks.testing import configure_app_for_test, make_settings
from druks.user_settings.models import SettingsOverride
from fastapi.testclient import TestClient
from sqlalchemy import text


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
    claude_model_ids = [m["id"] for m in harnesses["claude"]["allowedModels"]]
    assert "claude-sonnet-4-6" in claude_model_ids
    assert harnesses["codex"]["provider"] == "openai"
    assert (harnesses["codex"]["effort"], harnesses["codex"]["timeout"]) == ("high", 1800)


def test_harness_response_carries_connection_state(tmp_path: Path):
    with _build_client(tmp_path) as client:
        claude = {h["name"]: h for h in client.get("/api/settings/harnesses").json()}["claude"]
    assert claude["connected"] is False
    assert not claude["account"]
    assert not claude["providerEmail"]
    assert "expiresAt" in claude


async def test_harnesses_show_only_the_requesting_accounts_connection(tmp_path: Path, druks_db):
    from conftest import connect_harness
    from druks.harnesses.claude import ClaudeHarness

    # The suite's identity gate stands in op@example.com; another account's
    # connection never shows on this card.
    await connect_harness(
        ClaudeHarness,
        {"claudeAiOauth": {"accessToken": "x"}},
        provider_email="someone-else@example.com",
    )
    with _build_client(tmp_path) as client:
        claude = {h["name"]: h for h in client.get("/api/settings/harnesses").json()}["claude"]
    assert claude["connected"] is False


async def test_harness_card_reports_identity(tmp_path: Path, druks_db):
    from druks.accounts.models import Account
    from druks.harnesses.models import HarnessConnection

    # The provider identity is display, never authority.
    await HarnessConnection.connect(
        harness="claude",
        account=await Account.get_or_create("op@example.com"),
        payload={"claudeAiOauth": {"accessToken": "x"}},
        expires_at=None,
        provider_email="seat@corp.com",
    )
    with _build_client(tmp_path) as client:
        claude = {h["name"]: h for h in client.get("/api/settings/harnesses").json()}["claude"]
    assert claude["connected"] is True
    assert claude["account"] == "op@example.com"
    assert claude["providerEmail"] == "seat@corp.com"


async def test_harness_card_reads_expired_token_as_not_connected(tmp_path: Path, druks_db):
    from datetime import UTC, datetime, timedelta

    from druks.accounts.models import Account
    from druks.harnesses.models import HarnessConnection

    await HarnessConnection.connect(
        harness="claude",
        account=await Account.get_or_create("op@example.com"),
        payload={"claudeAiOauth": {"accessToken": "x"}},
        expires_at=datetime.now(UTC) - timedelta(hours=1),
        provider_email="seat@corp.com",
    )
    with _build_client(tmp_path) as client:
        claude = {h["name"]: h for h in client.get("/api/settings/harnesses").json()}["claude"]
    assert claude["connected"] is False


async def test_disconnect_removes_only_the_requesting_accounts_connection(tmp_path: Path, druks_db):
    from conftest import connect_harness
    from druks.harnesses.claude import ClaudeHarness
    from druks.harnesses.models import HarnessConnection

    mine = await connect_harness(ClaudeHarness, {"claudeAiOauth": {"accessToken": "x"}})
    other = await connect_harness(
        ClaudeHarness,
        {"claudeAiOauth": {"accessToken": "y"}},
        provider_email="someone-else@example.com",
    )
    mine_id, other_id = mine.id, other.id
    with _build_client(tmp_path) as client:
        response = client.delete("/api/harnesses/claude/connection")
    assert response.status_code == 200
    assert response.json()["connected"] is False
    # The request deleted in its own task-scoped session; read past this
    # task's identity map for what actually persisted.
    assert not await HarnessConnection.reload(mine_id)
    assert await HarnessConnection.reload(other_id)


def test_disconnect_without_a_connection_is_a_no_op(tmp_path: Path):
    with _build_client(tmp_path) as client:
        response = client.delete("/api/harnesses/claude/connection")
    assert response.status_code == 200
    assert response.json()["connected"] is False


def test_patch_settings_persists_valid_iana_zone(tmp_path: Path, monkeypatch):
    async def _noop_schedules():
        return None

    monkeypatch.setattr("druks.user_settings.routes.apply_schedules", _noop_schedules)
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

    async def record():
        reconciled.append(True)

    monkeypatch.setattr("druks.user_settings.routes.apply_schedules", record)
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


def _software_factory_app(client: TestClient) -> dict:
    body = client.get("/api/settings/apps").json()
    return next(m for m in body["apps"] if m["name"] == "software_factory")


def _software_factory_settings_fields(client: TestClient) -> dict:
    return {field["name"]: field for field in _software_factory_app(client)["settings"]}


def _review_app(client: TestClient) -> dict:
    body = client.get("/api/settings/apps").json()
    return next(m for m in body["apps"] if m["name"] == "review")


def _review_settings_fields(client: TestClient) -> dict:
    return {field["name"]: field for field in _review_app(client)["settings"]}


def test_apps_surface_build_agents(tmp_path: Path):
    """The build pipeline's agents all tune under the SoftwareFactory app."""
    with _build_client(tmp_path) as client:
        body = client.get("/api/settings/apps").json()
    apps = {m["name"]: m for m in body["apps"]}

    build_agents = {a["name"]: a for a in apps["software_factory"]["agents"]}
    # The build pipeline's plan stage stays; the standalone Plan-tab agent is gone.
    assert "generate_plan" in build_agents
    assert "planning" not in build_agents


def test_apps_surface_build_agents_and_workflow_defaults(tmp_path: Path):
    with _build_client(tmp_path) as client:
        build = _software_factory_app(client)

    agents = {a["name"]: a for a in build["agents"]}
    # An agent's family-token default resolves to the family's model; effort
    # and timeout inherit the global defaults ("high", 1800s) when the agent
    # declares neither and the operator set no override.
    assert agents["generate_plan"] == {
        "name": "generate_plan",
        "description": "ticket → implementation plan",
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
    assert fields["plan_gate"] == {
        "name": "plan_gate",
        "label": "Plan gate",
        "help": (
            "human — Operator reviews every plan; the machine reviewer never runs. "
            "machine — The machine reviewer critiques once; the plan implements without "
            "operator review. machine_then_human — The machine reviewer critiques once, "
            "then the operator approves every plan. adaptive — The machine reviewer "
            "critiques once; a high-confidence plan it approved implements directly, "
            "anything less parks for the operator."
        ),
        "type": "enum",
        "value": "human",
        "default": "human",
        "choices": ["human", "machine", "machine_then_human", "adaptive"],
        "section": "",
        "visibleWhenField": "",
        "visibleWhenValue": None,
        "secretSet": None,
        "multiline": False,
        "overridden": False,
    }


async def test_app_secret_round_trip_encrypts_at_rest(tmp_path: Path):
    secret = "review-pem-value"
    app_id = "42424242"
    key = "app:review:private_key"
    with _build_client(tmp_path) as client:
        written = client.patch(
            "/api/settings/apps",
            json={
                "appSettings": {
                    "review": {
                        "app_id": app_id,
                        "private_key": secret,
                    }
                }
            },
        )
        stored = (
            await db_session().execute(
                text(
                    "SELECT value, value IS NULL AS value_is_null, secret_value "
                    "FROM settings_overrides WHERE key = :key"
                ),
                {"key": key},
            )
        ).one()
        read = client.get("/api/settings/apps")
        resolved = (await Review.settings()).private_key

    assert written.status_code == 200
    assert read.status_code == 200
    assert stored.value is None
    assert stored.value_is_null is True
    assert stored.secret_value
    assert secret.encode() not in stored.secret_value
    assert secret not in written.text
    assert secret not in read.text
    assert app_id not in written.text
    assert app_id not in read.text
    assert resolved and resolved.get_secret_value() == secret
    review = next(app for app in read.json()["apps"] if app["name"] == "review")
    fields = {field["name"]: field for field in review["settings"]}
    assert fields["private_key"]["type"] == "secret"
    assert fields["private_key"]["value"] is None
    assert fields["private_key"]["default"] is None
    assert fields["private_key"]["secretSet"] is True
    assert fields["private_key"]["overridden"] is True
    assert fields["app_id"]["secretSet"] is True


async def test_app_secret_plaintext_row_is_unset_until_resaved(tmp_path: Path):
    secret = "legacy-plaintext-secret"
    key = "app:review:private_key"
    db_session().add(SettingsOverride(key=key, value=secret))
    await db_session().flush()

    with _build_client(tmp_path) as client:
        initial = _review_app(client)
        resolved_initial = (await Review.settings()).private_key
        saved = client.patch(
            "/api/settings/apps",
            json={
                "appSettings": {
                    "review": {
                        "app_id": "42",
                        "private_key": secret,
                    }
                }
            },
        )
        stored = (
            await db_session().execute(
                text(
                    "SELECT value, value IS NULL AS value_is_null, secret_value "
                    "FROM settings_overrides WHERE key = :key"
                ),
                {"key": key},
            )
        ).one()

    initial_field = next(
        setting for setting in initial["settings"] if setting["name"] == "private_key"
    )
    assert initial_field["secretSet"] is False
    assert not resolved_initial
    assert saved.status_code == 200
    assert stored.value is None
    assert stored.value_is_null is True
    assert stored.secret_value
    assert secret.encode() not in stored.secret_value


async def test_app_non_secret_setting_stays_in_value(tmp_path: Path):
    status = "Agent Queue"
    key = "app:software_factory:linear_trigger_status"

    with _build_client(tmp_path) as client:
        written = client.patch(
            "/api/settings/apps",
            json={"appSettings": {"software_factory": {"linear_trigger_status": status}}},
        )
        stored = (
            await db_session().execute(
                text("SELECT value, secret_value FROM settings_overrides WHERE key = :key"),
                {"key": key},
            )
        ).one()
        software_factory = _software_factory_app(client)

    field = next(
        setting
        for setting in software_factory["settings"]
        if setting["name"] == "linear_trigger_status"
    )
    assert written.status_code == 200
    assert stored.value == status
    assert stored.secret_value == b""
    assert (await SoftwareFactory.settings()).linear_trigger_status == status
    assert field["value"] == status
    assert field["overridden"] is True


def test_incoherent_app_save_is_rejected_and_rolled_back_before_schedules(
    tmp_path: Path, monkeypatch
):
    reconciled = []

    async def record():
        reconciled.append(True)

    monkeypatch.setattr("druks.user_settings.routes.apply_schedules", record)
    with _build_client(tmp_path) as client:
        response = client.patch(
            "/api/settings/apps",
            json={
                "agentModels": {"generate_plan": "claude-opus-4-7"},
                "appSettings": {"review": {"app_id": "42"}},
            },
        )

        assert response.status_code == 422
        assert response.json()["detail"] == {
            "review": {"private_key": "Required once the review App ID is set."}
        }
        assert not reconciled
        assert _review_settings_fields(client)["app_id"]["secretSet"] is False
        agents = {agent["name"]: agent for agent in _software_factory_app(client)["agents"]}
        assert agents["generate_plan"]["model"] == "gpt-5.5"


async def test_clearing_the_identity_deletes_its_overrides_and_stays_coherent(tmp_path: Path):
    key = "app:review:app_id"

    with _build_client(tmp_path) as client:
        configured = client.patch(
            "/api/settings/apps",
            json={
                "appSettings": {
                    "review": {
                        "app_id": "42",
                        "private_key": "review-pem",
                    }
                }
            },
        )
        cleared = client.patch(
            "/api/settings/apps",
            json={"appSettings": {"review": {"app_id": None, "private_key": None}}},
        )
        stored = (
            await db_session().execute(
                text("SELECT 1 FROM settings_overrides WHERE key = :key"),
                {"key": key},
            )
        ).one_or_none()
        fields = _review_settings_fields(client)

    assert configured.status_code == 200
    assert cleared.status_code == 200
    assert stored is None
    assert not (await Review.settings()).app_id
    assert fields["app_id"]["secretSet"] is False
    assert fields["private_key"]["secretSet"] is False


def test_apps_override_agent_model_persists(tmp_path: Path):
    with _build_client(tmp_path) as client:
        patch = client.patch(
            "/api/settings/apps",
            json={"agentModels": {"implement": "gpt-5.5"}},
        )
        assert patch.status_code == 200
        agents = {a["name"]: a for a in _software_factory_app(client)["agents"]}

    assert agents["implement"]["model"] == "gpt-5.5"
    assert agents["implement"]["source"] == "agent"


def test_apps_harness_effort_and_per_agent_effort_override(tmp_path: Path):
    with _build_client(tmp_path) as client:
        agents = {a["name"]: a for a in _software_factory_app(client)["agents"]}
        # generate_plan runs on codex and inherits the codex harness effort.
        assert agents["generate_plan"]["effort"] == "high"
        assert agents["generate_plan"]["effortSource"] == "harness"

        # Retune the codex harness effort + override one agent.
        client.patch("/api/settings/harnesses/codex", json={"effort": "low"})
        client.patch("/api/settings/apps", json={"agentEfforts": {"generate_plan": "high"}})
        agents = {a["name"]: a for a in _software_factory_app(client)["agents"]}
        # generate_plan overridden; revise_contract (also codex) inherits "low".
        assert agents["generate_plan"]["effort"] == "high"
        assert agents["generate_plan"]["effortSource"] == "agent"
        assert agents["revise_contract"]["effort"] == "low"
        assert agents["revise_contract"]["effortSource"] == "harness"


def test_apps_reject_unknown_effort(tmp_path: Path):
    with _build_client(tmp_path) as client:
        response = client.patch(
            "/api/settings/apps",
            json={"agentEfforts": {"implement": "turbo"}},
        )
    assert response.status_code == 422
    assert "turbo" in response.json()["detail"]


def test_apps_harness_timeout_and_per_agent_timeout_override(tmp_path: Path):
    with _build_client(tmp_path) as client:
        agents = {a["name"]: a for a in _software_factory_app(client)["agents"]}
        # implement runs on claude and inherits the claude harness timeout.
        assert agents["implement"]["timeout"] == 1800
        assert agents["implement"]["timeoutSource"] == "harness"

        # Retune the claude harness timeout + override one agent.
        client.patch("/api/settings/harnesses/claude", json={"timeout": 1200})
        client.patch("/api/settings/apps", json={"agentTimeouts": {"implement": 3600}})
        agents = {a["name"]: a for a in _software_factory_app(client)["agents"]}
        # implement overridden; review_plan (also claude) inherits 1200.
        assert agents["implement"]["timeout"] == 3600
        assert agents["implement"]["timeoutSource"] == "agent"
        assert agents["review_plan"]["timeout"] == 1200
        assert agents["review_plan"]["timeoutSource"] == "harness"


def test_apps_reject_non_positive_timeout(tmp_path: Path):
    with _build_client(tmp_path) as client:
        response = client.patch(
            "/api/settings/apps",
            json={"agentTimeouts": {"implement": 0}},
        )
    assert response.status_code == 422


def test_build_review_code_is_a_workflow_setting(tmp_path: Path):
    """Gating the code reviewer is a build-workflow boolean, not an agent flag."""
    with _build_client(tmp_path) as client:
        workflow = _software_factory_app(client)["workflows"][0]
        fields = {f["name"]: f for f in workflow["fields"]}
        assert fields["review_code"]["value"] is True
        assert fields["review_code"]["overridden"] is False

        patch = client.patch(
            "/api/settings/apps",
            json={"workflowSettings": {workflow["kind"]: {"review_code": False}}},
        )
        assert patch.status_code == 200
        fields = {f["name"]: f for f in _software_factory_app(client)["workflows"][0]["fields"]}
        assert fields["review_code"]["value"] is False
        assert fields["review_code"]["overridden"] is True


def test_apps_clearing_an_override_reverts_to_the_family_default(tmp_path: Path):
    with _build_client(tmp_path) as client:
        client.patch(
            "/api/settings/apps", json={"agentModels": {"generate_plan": "claude-opus-4-7"}}
        )
        agents = {a["name"]: a for a in _software_factory_app(client)["agents"]}
        assert agents["generate_plan"]["model"] == "claude-opus-4-7"
        assert agents["generate_plan"]["source"] == "agent"

        # Null clears the override; the agent falls back to its family default.
        client.patch("/api/settings/apps", json={"agentModels": {"generate_plan": None}})
        agents = {a["name"]: a for a in _software_factory_app(client)["agents"]}
        assert agents["generate_plan"]["model"] == "gpt-5.5"
        assert agents["generate_plan"]["source"] == "default"


def test_apps_reject_unknown_agent_model(tmp_path: Path):
    with _build_client(tmp_path) as client:
        # No installed harness owns this namespace, so nothing could run it.
        response = client.patch(
            "/api/settings/apps",
            json={"agentModels": {"implement": "llama-3-70b"}},
        )
    assert response.status_code == 422
    assert "llama-3-70b" in response.json()["detail"]


def test_apps_override_workflow_setting_persists(tmp_path: Path):
    with _build_client(tmp_path) as client:
        patch = client.patch(
            "/api/settings/apps",
            json={
                "workflowSettings": {"software_factory.build": {"max_implementation_revisions": 8}}
            },
        )
        assert patch.status_code == 200
        fields = {f["name"]: f for f in _software_factory_app(client)["workflows"][0]["fields"]}

    assert fields["max_implementation_revisions"]["value"] == 8
    assert fields["max_implementation_revisions"]["overridden"] is True


def test_apps_plan_gate_override_persists(tmp_path: Path):
    with _build_client(tmp_path) as client:
        patch = client.patch(
            "/api/settings/apps",
            json={
                "workflowSettings": {"software_factory.build": {"plan_gate": "machine_then_human"}}
            },
        )
        assert patch.status_code == 200
        fields = {f["name"]: f for f in _software_factory_app(client)["workflows"][0]["fields"]}

    assert fields["plan_gate"]["value"] == "machine_then_human"
    assert fields["plan_gate"]["overridden"] is True


def test_apps_reject_removed_auto_dispatch_setting(tmp_path: Path):
    with _build_client(tmp_path) as client:
        response = client.patch(
            "/api/settings/apps",
            json={
                "workflowSettings": {
                    "software_factory.build": {"auto_dispatch_on_plan_approval": True}
                }
            },
        )

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail == "Unknown software_factory.build setting 'auto_dispatch_on_plan_approval'"


def test_apps_reject_out_of_range_workflow_setting(tmp_path: Path):
    with _build_client(tmp_path) as client:
        response = client.patch(
            "/api/settings/apps",
            json={
                "workflowSettings": {"software_factory.build": {"max_implementation_revisions": 99}}
            },
        )
    assert response.status_code == 422

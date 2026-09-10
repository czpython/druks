from pathlib import Path

from conftest import settings_client
from druks.accounts.models import Account
from druks.contrib.software_factory.app import SoftwareFactory
from druks.database import db_session
from druks.user_settings.models import SettingsOverride
from druks_field_notes.app import FieldNotes
from fastapi.testclient import TestClient
from sqlalchemy import text


def test_get_settings_returns_shared_defaults(tmp_path: Path):
    with settings_client(tmp_path) as client:
        response = client.get("/api/settings")

    assert response.status_code == 200
    body = response.json()
    assert "timezone" not in body
    assert "updatedAt" in body


def test_get_harnesses_lists_the_registry(tmp_path: Path):
    with settings_client(tmp_path) as client:
        harnesses = {h["name"]: h for h in client.get("/api/settings/harnesses").json()}
    assert list(harnesses) == ["claude", "codex", "opencode", "pi"]
    assert harnesses["claude"] == {
        "name": "claude",
        "provider": "anthropic",
        "billingOptions": ["api_key", "subscription"],
    }
    assert harnesses["codex"]["provider"] == "openai"
    assert harnesses["pi"] == {"name": "pi", "provider": None, "billingOptions": ["api_key"]}


def test_get_settings_carries_the_execution_defaults(tmp_path: Path):
    with settings_client(tmp_path) as client:
        body = client.get("/api/settings").json()
    assert body["defaultHarness"] == "claude"
    assert body["defaultModel"] == "anthropic/claude-opus-4-7"
    assert body["defaultBilling"] == "subscription"
    assert (body["defaultEffort"], body["fastMode"], body["defaultTimeout"]) == (
        "high",
        False,
        1800,
    )
    assert "accountId" not in body


def test_patch_settings_judges_the_default_triple_together(tmp_path: Path):
    with settings_client(tmp_path) as client:
        # opencode takes keys only: the harness alone does not fit the
        # subscription default it would inherit.
        alone = client.patch("/api/settings", json={"defaultHarness": "opencode"})
        assert alone.status_code == 422
        assert "API key only" in alone.json()["detail"]
        together = client.patch(
            "/api/settings", json={"defaultHarness": "opencode", "defaultBilling": "api_key"}
        )
        assert together.status_code == 200
        assert (together.json()["defaultHarness"], together.json()["defaultBilling"]) == (
            "opencode",
            "api_key",
        )
        outside = client.patch("/api/settings", json={"defaultModel": "openai/gpt-5.5"})
        assert outside.status_code == 200  # opencode runs any key vendor
        codex = client.patch("/api/settings", json={"defaultHarness": "claude"})
        assert codex.status_code == 422
        assert "does not run OpenAI" in codex.json()["detail"]


async def test_accounts_report_the_default_without_a_fallback_setting(tmp_path: Path, druks_db):
    account = await Account.get_or_create("ops@example.com")
    with settings_client(tmp_path) as client:
        assert {"id": account.id, "username": "ops@example.com", "isDefault": True} in client.get(
            "/api/auth/accounts"
        ).json()
        assert (
            client.patch("/api/settings", json={"fallbackAccountId": account.id}).status_code == 422
        )


def test_installation_timezone_cannot_be_changed_through_settings(tmp_path: Path):
    with settings_client(tmp_path) as client:
        assert client.patch("/api/settings", json={"timezone": "Europe/Madrid"}).status_code == 422
        response = client.patch("/api/settings/personal", json={"timezone": "Europe/Madrid"})
        assert response.status_code == 200
        assert client.get("/api/settings/personal").json()["timezone"] == "Europe/Madrid"


def test_patch_settings_updates_the_defaults_every_agent_inherits(tmp_path: Path):
    with settings_client(tmp_path) as client:
        patch = client.patch(
            "/api/settings",
            json={"defaultHarness": "codex", "defaultModel": "openai/gpt-5.5", "fastMode": True},
        )
        assert patch.status_code == 200
        assert patch.json()["defaultModel"] == "openai/gpt-5.5"
        agents = {a["name"]: a for a in _software_factory_app(client)["agents"]}
    assert agents["software_factory.implement"]["harness"] == "codex"
    assert agents["software_factory.implement"]["harnessSource"] == "default"
    assert agents["software_factory.implement"]["model"] == "openai/gpt-5.5"
    assert agents["software_factory.implement"]["source"] == "default"


def test_patch_settings_rejects_defaults_that_break_an_agent_override(tmp_path: Path):
    with settings_client(tmp_path) as client:
        override = client.patch(
            "/api/settings/apps",
            json={"agentModels": {"software_factory.generate_plan": "anthropic/claude-opus-4-7"}},
        )
        assert override.status_code == 200

        response = client.patch(
            "/api/settings",
            json={"defaultHarness": "codex", "defaultModel": "openai/gpt-5.5"},
        )

        assert response.status_code == 422
        assert "does not run Anthropic" in response.json()["detail"]
        assert client.get("/api/settings").json()["defaultHarness"] == "claude"


def test_patch_settings_rejects_a_model_no_harness_runs(tmp_path: Path):
    with settings_client(tmp_path) as client:
        response = client.patch("/api/settings", json={"defaultModel": "gpt-5.5"})
    assert response.status_code == 422
    assert "gpt-5.5" in response.json()["detail"]


def test_agents_lists_every_apps_agents_as_they_resolve(tmp_path: Path):
    with settings_client(tmp_path) as client:
        client.patch(
            "/api/settings/apps",
            json={
                "agentHarnesses": {"software_factory.implement": "codex"},
                "agentModels": {"software_factory.implement": "openai/gpt-5.5"},
            },
        )
        body = client.get("/api/agents").json()
    apps = {app["name"]: app for app in body["apps"]}
    assert "software_factory" in apps
    assert "field_notes" in apps
    agents = {a["name"]: a for a in apps["software_factory"]["agents"]}
    assert agents["software_factory.implement"]["harness"] == "codex"
    assert agents["software_factory.implement"]["harnessSource"] == "agent"
    assert agents["software_factory.implement"]["model"] == "openai/gpt-5.5"
    assert agents["software_factory.implement"]["source"] == "agent"
    assert agents["software_factory.implement"]["billing"] == "subscription"
    assert agents["software_factory.implement"]["billingSource"] == "default"
    assert agents["software_factory.generate_plan"]["harnessSource"] == "default"
    assert set(agents["software_factory.generate_plan"]) == {
        "name",
        "label",
        "description",
        "harness",
        "harnessSource",
        "model",
        "source",
        "billing",
        "billingSource",
        "effort",
        "effortSource",
        "timeout",
        "timeoutSource",
    }


def test_apps_judge_an_agents_triple_as_it_resolves(tmp_path: Path):
    with settings_client(tmp_path) as client:
        response = client.patch(
            "/api/settings/apps", json={"agentHarnesses": {"software_factory.implement": "pi"}}
        )
        assert response.status_code == 422
        assert "API key only" in response.json()["detail"]
        response = client.patch(
            "/api/settings/apps",
            json={"agentModels": {"software_factory.implement": "openai/gpt-5.5"}},
        )
        assert response.status_code == 422
        assert "does not run OpenAI" in response.json()["detail"]
        agents = {a["name"]: a for a in _software_factory_app(client)["agents"]}
        assert agents["software_factory.implement"]["harnessSource"] == "default"
        assert agents["software_factory.implement"]["source"] == "default"
        response = client.patch(
            "/api/settings/apps",
            json={
                "agentHarnesses": {"software_factory.implement": "pi"},
                "agentBillings": {"software_factory.implement": "api_key"},
            },
        )
        assert response.status_code == 200
        agents = {a["name"]: a for a in _software_factory_app(client)["agents"]}
        assert (
            agents["software_factory.implement"]["harness"],
            agents["software_factory.implement"]["billing"],
        ) == ("pi", "api_key")
        assert agents["software_factory.implement"]["billingSource"] == "agent"
        response = client.patch("/api/settings/apps", json={"agentBillings": {"ghost": "api_key"}})
        assert response.status_code == 422


def _software_factory_app(client: TestClient) -> dict:
    body = client.get("/api/settings/apps").json()
    return next(m for m in body["apps"] if m["name"] == "software_factory")


def _software_factory_settings_fields(client: TestClient) -> dict:
    return {field["name"]: field for field in _software_factory_app(client)["settings"]}


def _field_notes_app(client: TestClient) -> dict:
    body = client.get("/api/settings/apps").json()
    return next(m for m in body["apps"] if m["name"] == "field_notes")


def _field_notes_settings_fields(client: TestClient) -> dict:
    return {field["name"]: field for field in _field_notes_app(client)["settings"]}


def test_apps_surface_build_agents(tmp_path: Path):
    """The build pipeline's agents all tune under the SoftwareFactory app."""
    with settings_client(tmp_path) as client:
        body = client.get("/api/settings/apps").json()
    apps = {m["name"]: m for m in body["apps"]}

    build_agents = {a["name"]: a for a in apps["software_factory"]["agents"]}
    assert "software_factory.generate_plan" in build_agents
    assert "planning" not in build_agents


def test_apps_surface_build_agents_and_workflow_defaults(tmp_path: Path):
    with settings_client(tmp_path) as client:
        build = _software_factory_app(client)

    agents = {a["name"]: a for a in build["agents"]}
    assert agents["software_factory.generate_plan"] == {
        "name": "software_factory.generate_plan",
        "label": "generate_plan",
        "description": "ticket → implementation plan",
        "harness": "claude",
        "harnessSource": "default",
        "model": "anthropic/claude-opus-4-7",
        "source": "default",
        "billing": "subscription",
        "billingSource": "default",
        "effort": "high",
        "effortSource": "default",
        "timeout": 1800,
        "timeoutSource": "default",
    }
    assert agents["software_factory.implement"]["model"] == "anthropic/claude-opus-4-7"
    assert agents["software_factory.evaluate_implementation"]["effortSource"] == "default"
    fields = {f["name"]: f for f in build["workflows"][0]["fields"]}
    assert fields["max_implementation_revisions"]["value"] == 5
    assert fields["plan_gate"] == {
        "name": "plan_gate",
        "label": "Plan gate",
        "help": "Choose who approves the plan before implementation.",
        "choiceDetails": {
            "human": {
                "label": "Human review",
                "help": "You approve every plan. The machine reviewer does not run.",
            },
            "machine": {
                "label": "Machine review",
                "help": (
                    "The machine reviewer checks once. Implementation starts without your approval."
                ),
            },
            "machine_then_human": {
                "label": "Machine then human",
                "help": "The machine reviewer checks once. You then approve the plan.",
            },
            "adaptive": {
                "label": "Adaptive review",
                "help": (
                    "An approved high-confidence plan starts directly. "
                    "All other plans need your approval."
                ),
            },
        },
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
    secret = "signing-pem-value"
    token = "sk-42424242"
    key = "app:field_notes:sync_signing_key"
    with settings_client(tmp_path) as client:
        written = client.patch(
            "/api/settings/apps",
            json={
                "appSettings": {
                    "field_notes": {
                        "visibility": "public",
                        "sync_token": token,
                        "sync_signing_key": secret,
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
        resolved = (await FieldNotes.settings()).sync_signing_key

    assert written.status_code == 200
    assert read.status_code == 200
    assert stored.value is None
    assert stored.value_is_null is True
    assert stored.secret_value
    assert secret.encode() not in stored.secret_value
    assert secret not in written.text
    assert secret not in read.text
    assert token not in written.text
    assert token not in read.text
    assert resolved and resolved.get_secret_value() == secret
    field_notes = next(app for app in read.json()["apps"] if app["name"] == "field_notes")
    fields = {field["name"]: field for field in field_notes["settings"]}
    assert fields["sync_signing_key"]["type"] == "secret"
    assert fields["sync_signing_key"]["value"] is None
    assert fields["sync_signing_key"]["default"] is None
    assert fields["sync_signing_key"]["secretSet"] is True
    assert fields["sync_signing_key"]["overridden"] is True
    assert fields["sync_token"]["secretSet"] is True


async def test_app_secret_plaintext_row_is_unset_until_resaved(tmp_path: Path):
    secret = "legacy-plaintext-secret"
    key = "app:field_notes:sync_signing_key"
    db_session().add(SettingsOverride(key=key, value=secret))
    await db_session().flush()

    with settings_client(tmp_path) as client:
        initial = _field_notes_app(client)
        resolved_initial = (await FieldNotes.settings()).sync_signing_key
        saved = client.patch(
            "/api/settings/apps",
            json={"appSettings": {"field_notes": {"sync_signing_key": secret}}},
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
        setting for setting in initial["settings"] if setting["name"] == "sync_signing_key"
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

    with settings_client(tmp_path) as client:
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
    with settings_client(tmp_path) as client:
        response = client.patch(
            "/api/settings/apps",
            json={
                "agentModels": {"software_factory.generate_plan": "anthropic/claude-opus-4-7"},
                "appSettings": {"field_notes": {"visibility": "public"}},
            },
        )

        assert response.status_code == 422
        assert response.json()["detail"] == {
            "field_notes": {"sync_token": "Required when visibility is public."}
        }
        assert not reconciled
        assert _field_notes_settings_fields(client)["visibility"]["overridden"] is False
        agents = {agent["name"]: agent for agent in _software_factory_app(client)["agents"]}
        assert agents["software_factory.generate_plan"]["model"] == "anthropic/claude-opus-4-7"


async def test_clearing_a_secret_deletes_its_override_and_stays_coherent(tmp_path: Path):
    key = "app:field_notes:sync_token"

    with settings_client(tmp_path) as client:
        configured = client.patch(
            "/api/settings/apps",
            json={
                "appSettings": {
                    "field_notes": {
                        "visibility": "public",
                        "sync_token": "sk-42",
                        "sync_signing_key": "signing-pem",
                    }
                }
            },
        )
        cleared = client.patch(
            "/api/settings/apps",
            json={
                "appSettings": {
                    "field_notes": {
                        "visibility": "private",
                        "sync_token": None,
                        "sync_signing_key": None,
                    }
                }
            },
        )
        stored = (
            await db_session().execute(
                text("SELECT 1 FROM settings_overrides WHERE key = :key"),
                {"key": key},
            )
        ).one_or_none()
        fields = _field_notes_settings_fields(client)

    assert configured.status_code == 200
    assert cleared.status_code == 200
    assert stored is None
    assert not (await FieldNotes.settings()).sync_token
    assert fields["sync_token"]["secretSet"] is False
    assert fields["sync_signing_key"]["secretSet"] is False


def test_apps_override_agent_model_persists(tmp_path: Path):
    with settings_client(tmp_path) as client:
        patch = client.patch(
            "/api/settings/apps",
            json={
                "agentHarnesses": {"software_factory.implement": "codex"},
                "agentModels": {"software_factory.implement": "openai/gpt-5.5"},
            },
        )
        assert patch.status_code == 200
        agents = {a["name"]: a for a in _software_factory_app(client)["agents"]}

    assert agents["software_factory.implement"]["model"] == "openai/gpt-5.5"
    assert agents["software_factory.implement"]["source"] == "agent"


def test_apps_default_effort_and_per_agent_effort_override(tmp_path: Path):
    with settings_client(tmp_path) as client:
        agents = {a["name"]: a for a in _software_factory_app(client)["agents"]}
        assert agents["software_factory.generate_plan"]["effort"] == "high"
        assert agents["software_factory.generate_plan"]["effortSource"] == "default"

        client.patch("/api/settings", json={"defaultEffort": "low"})
        client.patch(
            "/api/settings/apps", json={"agentEfforts": {"software_factory.generate_plan": "high"}}
        )
        agents = {a["name"]: a for a in _software_factory_app(client)["agents"]}
        assert agents["software_factory.generate_plan"]["effort"] == "high"
        assert agents["software_factory.generate_plan"]["effortSource"] == "agent"
        assert agents["software_factory.revise_contract"]["effort"] == "low"
        assert agents["software_factory.revise_contract"]["effortSource"] == "default"


def test_apps_reject_unknown_effort(tmp_path: Path):
    with settings_client(tmp_path) as client:
        response = client.patch(
            "/api/settings/apps",
            json={"agentEfforts": {"software_factory.implement": "turbo"}},
        )
    assert response.status_code == 422
    assert "agentEfforts" in str(response.json()["detail"])


def test_apps_default_timeout_and_per_agent_timeout_override(tmp_path: Path):
    with settings_client(tmp_path) as client:
        agents = {a["name"]: a for a in _software_factory_app(client)["agents"]}
        assert agents["software_factory.implement"]["timeout"] == 1800
        assert agents["software_factory.implement"]["timeoutSource"] == "default"

        client.patch("/api/settings", json={"defaultTimeout": 1200})
        client.patch(
            "/api/settings/apps", json={"agentTimeouts": {"software_factory.implement": 3600}}
        )
        agents = {a["name"]: a for a in _software_factory_app(client)["agents"]}
        assert agents["software_factory.implement"]["timeout"] == 3600
        assert agents["software_factory.implement"]["timeoutSource"] == "agent"
        assert agents["software_factory.review_plan"]["timeout"] == 1200
        assert agents["software_factory.review_plan"]["timeoutSource"] == "default"


def test_apps_reject_non_positive_timeout(tmp_path: Path):
    with settings_client(tmp_path) as client:
        response = client.patch(
            "/api/settings/apps",
            json={"agentTimeouts": {"software_factory.implement": 0}},
        )
    assert response.status_code == 422


def test_build_review_code_is_a_workflow_setting(tmp_path: Path):
    """Gating the code reviewer is a build-workflow boolean, not an agent flag."""
    with settings_client(tmp_path) as client:
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


def test_apps_clearing_an_override_reverts_to_the_operator_default(tmp_path: Path):
    with settings_client(tmp_path) as client:
        client.patch(
            "/api/settings/apps",
            json={"agentModels": {"software_factory.generate_plan": "anthropic/claude-opus-4-7"}},
        )
        agents = {a["name"]: a for a in _software_factory_app(client)["agents"]}
        assert agents["software_factory.generate_plan"]["model"] == "anthropic/claude-opus-4-7"
        assert agents["software_factory.generate_plan"]["source"] == "agent"

        client.patch(
            "/api/settings/apps", json={"agentModels": {"software_factory.generate_plan": None}}
        )
        agents = {a["name"]: a for a in _software_factory_app(client)["agents"]}
        assert agents["software_factory.generate_plan"]["model"] == "anthropic/claude-opus-4-7"
        assert agents["software_factory.generate_plan"]["source"] == "default"


def test_apps_reject_unknown_agent_model(tmp_path: Path):
    with settings_client(tmp_path) as client:
        # No installed harness owns this namespace, so nothing could run it.
        response = client.patch(
            "/api/settings/apps",
            json={"agentModels": {"software_factory.implement": "llama-3-70b"}},
        )
    assert response.status_code == 422
    assert "llama-3-70b" in response.json()["detail"]


def test_apps_override_workflow_setting_persists(tmp_path: Path):
    with settings_client(tmp_path) as client:
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
    with settings_client(tmp_path) as client:
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
    with settings_client(tmp_path) as client:
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
    with settings_client(tmp_path) as client:
        response = client.patch(
            "/api/settings/apps",
            json={
                "workflowSettings": {"software_factory.build": {"max_implementation_revisions": 99}}
            },
        )
    assert response.status_code == 422

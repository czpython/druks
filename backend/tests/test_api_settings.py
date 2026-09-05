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


def test_get_harnesses_lists_the_registry(tmp_path: Path):
    with _build_client(tmp_path) as client:
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
    with _build_client(tmp_path) as client:
        body = client.get("/api/settings").json()
    assert body["defaultHarness"] == "claude"
    assert body["defaultModel"] == "anthropic/claude-opus-4-7"
    assert body["defaultBilling"] == "subscription"
    assert (body["defaultEffort"], body["fastMode"], body["defaultTimeout"]) == (
        "high",
        False,
        1800,
    )
    assert body["fallbackAccountId"] is None


def test_patch_settings_judges_the_default_triple_together(tmp_path: Path):
    with _build_client(tmp_path) as client:
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


async def test_patch_settings_sets_the_account_unattended_runs_run_as(tmp_path: Path, druks_db):
    from druks.accounts.models import Account

    account = await Account.get_or_create("ops@example.com")
    with _build_client(tmp_path) as client:
        assert {"id": account.id, "username": "ops@example.com"} in client.get(
            "/api/auth/accounts"
        ).json()
        patch = client.patch("/api/settings", json={"fallbackAccountId": account.id})
        assert patch.status_code == 200
        assert patch.json()["fallbackAccountId"] == account.id
        assert client.patch("/api/settings", json={"fallbackAccountId": "ghost"}).status_code == 422
        assert (
            client.patch("/api/settings", json={"fallbackAccountId": "system"}).status_code == 422
        )


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


def test_patch_settings_updates_the_defaults_every_agent_inherits(tmp_path: Path):
    with _build_client(tmp_path) as client:
        patch = client.patch(
            "/api/settings",
            json={"defaultHarness": "codex", "defaultModel": "openai/gpt-5.5", "fastMode": True},
        )
        assert patch.status_code == 200
        assert patch.json()["defaultModel"] == "openai/gpt-5.5"
        agents = {a["name"]: a for a in _software_factory_app(client)["agents"]}
    assert agents["implement"]["harness"] == "codex"
    assert agents["implement"]["harnessSource"] == "default"
    assert agents["implement"]["model"] == "openai/gpt-5.5"
    assert agents["implement"]["source"] == "default"


def test_patch_settings_rejects_defaults_that_break_an_agent_override(tmp_path: Path):
    with _build_client(tmp_path) as client:
        override = client.patch(
            "/api/settings/apps",
            json={"agentModels": {"generate_plan": "anthropic/claude-opus-4-7"}},
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
    with _build_client(tmp_path) as client:
        response = client.patch("/api/settings", json={"defaultModel": "gpt-5.5"})
    assert response.status_code == 422
    assert "gpt-5.5" in response.json()["detail"]


def test_agents_lists_every_apps_agents_as_they_resolve(tmp_path: Path):
    with _build_client(tmp_path) as client:
        client.patch(
            "/api/settings/apps",
            json={
                "agentHarnesses": {"implement": "codex"},
                "agentModels": {"implement": "openai/gpt-5.5"},
            },
        )
        body = client.get("/api/agents").json()
    apps = {app["name"]: app for app in body["apps"]}
    assert "software_factory" in apps
    assert "field_notes" in apps
    agents = {a["name"]: a for a in apps["software_factory"]["agents"]}
    assert agents["implement"]["harness"] == "codex"
    assert agents["implement"]["harnessSource"] == "agent"
    assert agents["implement"]["model"] == "openai/gpt-5.5"
    assert agents["implement"]["source"] == "agent"
    assert agents["implement"]["billing"] == "subscription"
    assert agents["implement"]["billingSource"] == "default"
    assert agents["generate_plan"]["harnessSource"] == "default"
    assert set(agents["generate_plan"]) == {
        "name",
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
    with _build_client(tmp_path) as client:
        # A key-only harness with the inherited subscription billing.
        response = client.patch("/api/settings/apps", json={"agentHarnesses": {"implement": "pi"}})
        assert response.status_code == 422
        assert "API key only" in response.json()["detail"]
        # A model outside the inherited harness's vendor.
        response = client.patch(
            "/api/settings/apps", json={"agentModels": {"implement": "openai/gpt-5.5"}}
        )
        assert response.status_code == 422
        assert "does not run OpenAI" in response.json()["detail"]
        # The rejected writes never landed.
        agents = {a["name"]: a for a in _software_factory_app(client)["agents"]}
        assert agents["implement"]["harnessSource"] == "default"
        assert agents["implement"]["source"] == "default"
        # Both cells together fit.
        response = client.patch(
            "/api/settings/apps",
            json={"agentHarnesses": {"implement": "pi"}, "agentBillings": {"implement": "api_key"}},
        )
        assert response.status_code == 200
        agents = {a["name"]: a for a in _software_factory_app(client)["agents"]}
        assert (agents["implement"]["harness"], agents["implement"]["billing"]) == ("pi", "api_key")
        assert agents["implement"]["billingSource"] == "agent"
        # An agent nobody registered.
        response = client.patch("/api/settings/apps", json={"agentBillings": {"ghost": "api_key"}})
        assert response.status_code == 422


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
    # Every cell inherits the operator's defaults when no override is set.
    assert agents["generate_plan"] == {
        "name": "generate_plan",
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
    assert agents["implement"]["model"] == "anthropic/claude-opus-4-7"
    assert agents["evaluate_implementation"]["effortSource"] == "default"
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
                "agentModels": {"generate_plan": "anthropic/claude-opus-4-7"},
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
        assert agents["generate_plan"]["model"] == "anthropic/claude-opus-4-7"


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
            json={
                "agentHarnesses": {"implement": "codex"},
                "agentModels": {"implement": "openai/gpt-5.5"},
            },
        )
        assert patch.status_code == 200
        agents = {a["name"]: a for a in _software_factory_app(client)["agents"]}

    assert agents["implement"]["model"] == "openai/gpt-5.5"
    assert agents["implement"]["source"] == "agent"


def test_apps_default_effort_and_per_agent_effort_override(tmp_path: Path):
    with _build_client(tmp_path) as client:
        agents = {a["name"]: a for a in _software_factory_app(client)["agents"]}
        assert agents["generate_plan"]["effort"] == "high"
        assert agents["generate_plan"]["effortSource"] == "default"

        # Retune the default effort + override one agent.
        client.patch("/api/settings", json={"defaultEffort": "low"})
        client.patch("/api/settings/apps", json={"agentEfforts": {"generate_plan": "high"}})
        agents = {a["name"]: a for a in _software_factory_app(client)["agents"]}
        # generate_plan overridden; revise_contract inherits "low".
        assert agents["generate_plan"]["effort"] == "high"
        assert agents["generate_plan"]["effortSource"] == "agent"
        assert agents["revise_contract"]["effort"] == "low"
        assert agents["revise_contract"]["effortSource"] == "default"


def test_apps_reject_unknown_effort(tmp_path: Path):
    with _build_client(tmp_path) as client:
        response = client.patch(
            "/api/settings/apps",
            json={"agentEfforts": {"implement": "turbo"}},
        )
    assert response.status_code == 422
    assert "agentEfforts" in str(response.json()["detail"])


def test_apps_default_timeout_and_per_agent_timeout_override(tmp_path: Path):
    with _build_client(tmp_path) as client:
        agents = {a["name"]: a for a in _software_factory_app(client)["agents"]}
        assert agents["implement"]["timeout"] == 1800
        assert agents["implement"]["timeoutSource"] == "default"

        # Retune the default timeout + override one agent.
        client.patch("/api/settings", json={"defaultTimeout": 1200})
        client.patch("/api/settings/apps", json={"agentTimeouts": {"implement": 3600}})
        agents = {a["name"]: a for a in _software_factory_app(client)["agents"]}
        # implement overridden; review_plan inherits 1200.
        assert agents["implement"]["timeout"] == 3600
        assert agents["implement"]["timeoutSource"] == "agent"
        assert agents["review_plan"]["timeout"] == 1200
        assert agents["review_plan"]["timeoutSource"] == "default"


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


def test_apps_clearing_an_override_reverts_to_the_operator_default(tmp_path: Path):
    with _build_client(tmp_path) as client:
        client.patch(
            "/api/settings/apps",
            json={"agentModels": {"generate_plan": "anthropic/claude-opus-4-7"}},
        )
        agents = {a["name"]: a for a in _software_factory_app(client)["agents"]}
        assert agents["generate_plan"]["model"] == "anthropic/claude-opus-4-7"
        assert agents["generate_plan"]["source"] == "agent"

        # Null clears the override; the agent falls back to the operator default.
        client.patch("/api/settings/apps", json={"agentModels": {"generate_plan": None}})
        agents = {a["name"]: a for a in _software_factory_app(client)["agents"]}
        assert agents["generate_plan"]["model"] == "anthropic/claude-opus-4-7"
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

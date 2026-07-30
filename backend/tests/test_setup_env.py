import stat
import tomllib
from pathlib import Path

import pytest
from druks.setup_env import GAPS_EXIT_CODE, MIGRATION_EXIT_CODE, read_env, run_setup


def _run(env_path: Path, **overrides):
    kwargs = {
        "provider": "exe",
        "install_dir": "/home/op/druks",
        "home": "/home/op",
        "interactive": False,
        "print_fn": lambda _line: None,
    }
    kwargs.update(overrides)
    return run_setup(env_path, **kwargs)


def _read_toml(path: Path) -> dict:
    with path.open("rb") as config_file:
        return tomllib.load(config_file)


def test_fresh_exe_render_matches_the_deployment_contract(tmp_path):
    env_path = tmp_path / ".env"

    assert _run(env_path) == GAPS_EXIT_CODE

    values = read_env(env_path)
    config = _read_toml(tmp_path / "druks.toml")
    assert values["DEFAULT_HOST_PROVIDER"] == "exe"
    assert values["TAILSCALE_ENABLED"] == "true"
    assert values["EXE_API_URL"] == "https://exe.dev"
    assert values["EXE_DEFAULT_IMAGE"] == "ghcr.io/boldsoftware/exeuntu:latest"
    assert values["DRUKS_AUTH_MODE"] == "header"
    assert values["DRUKS_AUTH_HEADER"] == "X-ExeDev-Email"
    assert values["SERVICE_TOKENS"] == values["DRUKS_SANDBOX_SERVICE_TOKEN"]
    assert values["GITHUB_OPERATOR_PEM"] == "/home/op/druks/secrets/operator.pem"
    assert values["DRUKS_DATA_DIR"] == "/home/op/druks-data"
    assert "EXE_API_TOKEN" not in values
    assert "TAILSCALE_TAILNET" not in values
    assert len(config["secrets"]["postgres_password"]) == 64
    assert len(config["secrets"]["webhook_secret"]) == 64
    assert len(config["secrets"]["sandbox_service_token"]) == 64
    assert (tmp_path / "secrets").is_dir()
    assert (tmp_path / ".gitignore").read_text().splitlines() == [
        "druks.toml",
        ".env",
        "secrets/",
    ]
    assert stat.S_IMODE(env_path.stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / "druks.toml").stat().st_mode) == 0o600


def test_generic_remote_passes_provider_environment_without_enumeration(tmp_path):
    env_path = tmp_path / ".env"

    def answer(prompt):
        if prompt.startswith("Identity header"):
            return "X-Forwarded-Email"
        return ""

    _run(
        env_path,
        provider="exoscale",
        interactive=True,
        input_fn=answer,
        set_values=("sandbox.env.EXOSCALE_API_KEY=key",),
    )

    values = read_env(env_path)
    assert values["DEFAULT_HOST_PROVIDER"] == "exoscale"
    assert values["DRUKS_AUTH_HEADER"] == "X-Forwarded-Email"
    assert values["EXOSCALE_API_KEY"] == "key"
    assert "AWS_REGION" not in values
    assert "EXE_API_TOKEN" not in values
    assert "TAILSCALE_ENABLED" not in values


def test_docker_shape_matches_local_wiring_and_ignores_sandbox_env(tmp_path):
    env_path = tmp_path / ".env"

    assert (
        _run(
            env_path,
            provider="docker",
            set_values=("sandbox.env.DOCKER_HOST=tcp://elsewhere",),
        )
        == GAPS_EXIT_CODE
    )

    values = read_env(env_path)
    assert values["DEFAULT_HOST_PROVIDER"] == "docker"
    assert values["DRUKS_SANDBOX_SERVICE_URL"] == "http://127.0.0.1:8000"
    assert values["DRUKS_SANDBOX_SERVICE_TOKEN"] == "dev-token"
    assert values["DRUKS_SANDBOX_IMAGE"] == "ghcr.io/czpython/druks-sandbox:latest"
    assert values["DRUKS_ENDPOINT"] == "http://localhost:8001"
    assert values["DRUKS_AUTH_MODE"] == "none"
    assert "DRUKS_AUTH_HEADER" not in values
    assert "SERVICE_TOKENS" not in values
    assert "DOCKER_HOST" not in values


def test_pre_toml_guard_refuses_without_writing(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text("DRUKS_POSTGRES_PASSWORD=keep\n")
    before = env_path.read_bytes()
    printed = []

    rc = _run(env_path, print_fn=printed.append)

    assert rc == MIGRATION_EXIT_CODE
    assert env_path.read_bytes() == before
    assert {path.name for path in tmp_path.iterdir()} == {".env"}
    output = "\n".join(printed)
    for key in (
        "DRUKS_POSTGRES_PASSWORD",
        "DRUKS_SECRETS_KEY",
        "DRUKS_WEBHOOK_SECRET",
        "DRUKS_SANDBOX_SERVICE_TOKEN",
        "GITHUB_*_APP_ID",
        "[sandbox.env]",
        "other hand-added .env keys → [env]",
    ):
        assert key in output


def test_set_updates_toml_and_rerender_preserves_the_values(tmp_path):
    env_path = tmp_path / ".env"
    _run(
        env_path,
        provider="docker",
        set_values=(
            "github.operator_app_id=101",
            "secrets.webhook_secret=github-secret",
        ),
    )

    printed = []
    _run(
        env_path,
        provider="exoscale",
        set_values=("secrets.webhook_secret=rotated-secret",),
        print_fn=printed.append,
    )

    config = _read_toml(tmp_path / "druks.toml")
    values = read_env(env_path)
    assert config["github"]["operator_app_id"] == "101"
    assert config["secrets"]["webhook_secret"] == "rotated-secret"
    assert config["sandbox"]["provider"] == "docker"
    assert values["GITHUB_OPERATOR_APP_ID"] == "101"
    assert values["DRUKS_WEBHOOK_SECRET"] == "rotated-secret"
    assert "docker compose up -d" in "\n".join(printed)


def test_generated_secrets_never_regenerate_on_rerun(tmp_path):
    env_path = tmp_path / ".env"
    _run(env_path)
    first = _read_toml(tmp_path / "druks.toml")["secrets"]

    _run(env_path, set_values=("github.operator_app_id=101",))

    assert _read_toml(tmp_path / "druks.toml")["secrets"] == first


def test_blank_toml_value_is_omitted_from_env(tmp_path):
    env_path = tmp_path / ".env"

    _run(env_path, set_values=("ticketing.linear_api_key=",))

    assert "LINEAR_API_KEY" not in read_env(env_path)
    assert "LINEAR_API_KEY=" not in env_path.read_text()


def test_deployment_env_addition_renders_verbatim_and_survives_rerender(tmp_path):
    env_path = tmp_path / ".env"
    _run(
        env_path,
        provider="docker",
        set_values=(
            "env.SLACK_SIGNING_SECRET=slack-secret",
            "env.DRUKS_SANDBOX_SERVICE_TIMEOUT=45",
        ),
    )

    _run(env_path)

    config = _read_toml(tmp_path / "druks.toml")
    assert config["env"]["SLACK_SIGNING_SECRET"] == "slack-secret"
    assert read_env(env_path)["SLACK_SIGNING_SECRET"] == "slack-secret"
    assert read_env(env_path)["DRUKS_SANDBOX_SERVICE_TIMEOUT"] == "45"
    assert "# DEPLOYMENT ENVIRONMENT ADDITIONS" in env_path.read_text()


def test_blank_deployment_env_addition_is_omitted(tmp_path):
    env_path = tmp_path / ".env"

    _run(env_path, provider="docker", set_values=("env.JIRA_API_TOKEN=",))

    assert "JIRA_API_TOKEN" not in read_env(env_path)
    assert "JIRA_API_TOKEN=" not in env_path.read_text()


def test_owned_deployment_env_key_is_a_named_gap_and_is_not_rendered(tmp_path):
    env_path = tmp_path / ".env"
    printed = []

    rc = _run(
        env_path,
        provider="docker",
        set_values=("env.DRUKS_ENDPOINT=wrong",),
        print_fn=printed.append,
    )

    assert rc == GAPS_EXIT_CODE
    assert "env.DRUKS_ENDPOINT is reserved by druks" in "\n".join(printed)
    assert read_env(env_path)["DRUKS_ENDPOINT"] == "http://localhost:8001"


def test_reserved_sandbox_env_key_is_a_named_gap_and_is_not_rendered(tmp_path):
    env_path = tmp_path / ".env"
    printed = []

    rc = _run(
        env_path,
        provider="exoscale",
        set_values=(
            "sandbox.env.EXOSCALE_API_KEY=key",
            "sandbox.env.DATABASE_URL=wrong",
        ),
        print_fn=printed.append,
    )

    assert rc == GAPS_EXIT_CODE
    assert "sandbox.env.DATABASE_URL is reserved by druks" in "\n".join(printed)
    assert read_env(env_path)["DATABASE_URL"] == "sqlite+aiosqlite:////data/drukbox.db"


def test_deleted_env_is_regenerated_byte_identically(tmp_path):
    env_path = tmp_path / ".env"
    _run(env_path)
    expected = env_path.read_bytes()
    env_path.unlink()

    _run(env_path)

    assert env_path.read_bytes() == expected


def test_compose_plane_env_additions_survive_rerender(tmp_path):
    env_path = tmp_path / ".env"
    _run(env_path, provider="docker")
    env_path.write_text(env_path.read_text() + "DRUKS_UID=1000\nCOMPOSE_PROFILES=\n")

    _run(env_path)

    values = read_env(env_path)
    assert values["DRUKS_UID"] == "1000"
    assert "COMPOSE_PROFILES" in values
    assert values["COMPOSE_PROFILES"] == ""
    assert "# OPERATOR ADDITIONS" in env_path.read_text()


def test_unknown_toml_values_survive_a_writer_pass(tmp_path):
    env_path = tmp_path / ".env"
    _run(env_path, provider="docker")
    toml_path = tmp_path / "druks.toml"
    body = toml_path.read_text()
    body = body.replace(
        'operator_app_id = ""',
        'operator_app_id = ""\ncustom_label = "blue"',
    )
    body += "\n[operator]\nretries = 4\nenabled = true\n"
    toml_path.write_text(body)

    _run(env_path, set_values=("github.operator_app_id=101",))

    config = _read_toml(toml_path)
    assert config["github"]["custom_label"] == "blue"
    assert config["operator"] == {"retries": 4, "enabled": True}
    assert "# OPERATOR ADDITIONS" in toml_path.read_text()


def test_missing_known_tables_are_canonicalized_before_rerender(tmp_path):
    env_path = tmp_path / ".env"
    _run(env_path, provider="docker")
    toml_path = tmp_path / "druks.toml"
    body = toml_path.read_text()
    body = body.replace("[ticketing]", "[ticketing-renamed]")
    body = body.replace("[sandbox.env]\n", "")
    body = body.replace('provider = "docker"\n', "")
    toml_path.write_text(body)
    printed = []

    rc = _run(
        env_path,
        set_values=("github.operator_app_id=101",),
        print_fn=printed.append,
    )

    config = _read_toml(toml_path)
    assert rc == GAPS_EXIT_CODE
    assert "DEFAULT_HOST_PROVIDER is empty" in "\n".join(printed)
    assert "GITHUB_REVIEWER_APP_ID is empty" in "\n".join(printed)
    assert config["ticketing"] == {"linear_api_key": "", "linear_webhook_secret": ""}
    assert "ticketing-renamed" in config
    assert config["sandbox"]["env"] == {}


def test_nested_operator_content_is_refused(tmp_path):
    """Operator additions are flat scalars, one table deep — druks.toml is
    druks' file, and structure it can't own round-trip is refused, not
    silently re-rendered."""
    env_path = tmp_path / ".env"
    _run(env_path, provider="docker")
    toml_path = tmp_path / "druks.toml"
    toml_path.write_text(toml_path.read_text() + "\n[operator.nested]\nvalue = 1\n")

    with pytest.raises(ValueError, match="operator.nested"):
        _run(env_path)


def test_interactive_rerun_prompts_only_for_required_blanks(tmp_path):
    env_path = tmp_path / ".env"

    def answer(prompt):
        answers = {
            "Operator GitHub App": "101",
            "Reviewer GitHub App": "202",
            "exe.dev API token": "exe-token",
            "Tailscale magic-DNS": "tail.ts.net",
        }
        return next((value for label, value in answers.items() if label in prompt), "")

    _run(env_path, interactive=True, input_fn=answer)
    (tmp_path / "secrets" / "operator.pem").write_text("pem")
    (tmp_path / "secrets" / "reviewer.pem").write_text("pem")

    def fail_on_prompt(prompt):
        raise AssertionError(f"unexpected prompt: {prompt}")

    assert _run(env_path, interactive=True, input_fn=fail_on_prompt) == 0


def test_fresh_interactive_run_prompts_for_provider_first(tmp_path):
    env_path = tmp_path / ".env"
    prompts = []

    def answer(prompt):
        prompts.append(prompt)
        if len(prompts) == 1:
            return "docker"
        return ""

    _run(env_path, provider="exe", interactive=True, input_fn=answer)

    assert prompts[0] == "Sandbox provider [exe]: "
    assert _read_toml(tmp_path / "druks.toml")["sandbox"]["provider"] == "docker"

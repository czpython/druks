import stat
import tomllib
from pathlib import Path

import pytest
from druks.settings import Settings
from druks.setup_env import GAPS_EXIT_CODE, MIGRATION_EXIT_CODE, read_env, run_setup

DROPPED_RENDER_KEYS = {
    "DRUKS_AUTH_MODE": ("identity.mode", "jwt"),
    "DRUKS_AUTH_JWKS_URL": ("identity.jwks_url", "https://edge.example/jwks"),
    "DRUKS_AUTH_JWT_ISSUER": ("identity.jwt_issuer", "https://edge.example"),
    "DRUKS_AUTH_JWT_AUDIENCE": ("identity.jwt_audience", "druks"),
    "DRUKS_AUTH_JWT_IDENTITY_CLAIM": ("identity.jwt_identity_claim", "sub"),
    "DRUKS_ENDPOINT": ("urls.endpoint", "https://druks.example"),
    "DRUKS_WEBHOOK_SECRET": ("secrets.webhook_secret", "webhook-secret"),
    "DRUKS_SECRETS_KEY": ("secrets.secrets_key", "secrets-key"),
    "GITHUB_OPERATOR_APP_ID": ("github.operator_app_id", "101"),
    "GITHUB_REVIEWER_APP_ID": ("github.reviewer_app_id", "202"),
    "GITHUB_OPERATOR_PRIVATE_KEY_PATH": ("github.operator_pem", "operator.pem"),
    "GITHUB_REVIEWER_PRIVATE_KEY_PATH": ("github.reviewer_pem", "reviewer.pem"),
    "DRUKS_SANDBOX_SERVICE_URL": ("sandbox.service_url", "http://sandbox:8000"),
    "DRUKS_SANDBOX_SERVICE_TOKEN": ("sandbox.service_token", "token"),
    "DRUKS_SANDBOX_IMAGE": ("sandbox.image", "sandbox:latest"),
}


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
    assert values["DRUKS_AUTH_HEADER"] == "X-ExeDev-Email"
    assert values["SERVICE_TOKENS"] == config["sandbox"]["service_token"]
    assert values["GITHUB_OPERATOR_PEM"] == "/home/op/druks/secrets/operator.pem"
    assert values["DRUKS_DATA_DIR"] == "/home/op/druks-data"
    assert "EXE_API_TOKEN" not in values
    assert "TAILSCALE_TAILNET" not in values
    assert len(config["secrets"]["postgres_password"]) == 64
    assert len(config["secrets"]["webhook_secret"]) == 64
    assert len(config["sandbox"]["service_token"]) == 64
    assert (tmp_path / "secrets").is_dir()
    assert (tmp_path / ".gitignore").read_text().splitlines() == [
        "druks.toml",
        ".env",
        "secrets/",
    ]
    assert stat.S_IMODE(env_path.stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / "druks.toml").stat().st_mode) == 0o600


@pytest.mark.parametrize(
    ("rendered_key", "assignment"),
    [
        (rendered_key, f"{toml_path}={value}")
        for rendered_key, (toml_path, value) in DROPPED_RENDER_KEYS.items()
    ],
)
def test_toml_application_setting_is_not_rendered(tmp_path, rendered_key, assignment):
    env_path = tmp_path / ".env"

    _run(env_path, provider="docker", set_values=(assignment,))

    assert rendered_key not in read_env(env_path)


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
        set_values=("sandbox.exoscale.EXOSCALE_API_KEY=key",),
    )

    values = read_env(env_path)
    assert values["DEFAULT_HOST_PROVIDER"] == "exoscale"
    assert values["DRUKS_AUTH_HEADER"] == "X-Forwarded-Email"
    assert values["EXOSCALE_API_KEY"] == "key"
    assert "AWS_REGION" not in values
    assert "EXE_API_TOKEN" not in values
    assert "TAILSCALE_ENABLED" not in values


def test_docker_shape_matches_local_wiring_and_ignores_provider_environment(tmp_path):
    env_path = tmp_path / ".env"

    assert (
        _run(
            env_path,
            provider="docker",
            set_values=("sandbox.docker.DOCKER_HOST=tcp://elsewhere",),
        )
        == GAPS_EXIT_CODE
    )

    values = read_env(env_path)
    assert values["DEFAULT_HOST_PROVIDER"] == "docker"
    assert "DRUKS_AUTH_HEADER" not in values
    assert "SERVICE_TOKENS" not in values
    assert "DOCKER_HOST" not in values


def test_exe_provider_environment_renders_verbatim(tmp_path):
    env_path = tmp_path / ".env"

    _run(
        env_path,
        set_values=(
            "sandbox.exe.EXE_API_TOKEN=exe-token",
            "sandbox.exe.TAILSCALE_TAILNET=tail.ts.net",
            "sandbox.exe.CUSTOM_DRUKBOX_VALUE=verbatim",
        ),
    )

    values = read_env(env_path)
    assert values["EXE_API_TOKEN"] == "exe-token"
    assert values["TAILSCALE_TAILNET"] == "tail.ts.net"
    assert values["CUSTOM_DRUKBOX_VALUE"] == "verbatim"


def test_legacy_sandbox_table_is_renamed_to_the_provider_table(tmp_path):
    env_path = tmp_path / ".env"
    _run(env_path)
    toml_path = tmp_path / "druks.toml"
    toml_path.write_text(
        toml_path.read_text().replace(
            "[sandbox.exe]",
            "# credentials for this provider\n[sandbox.env]",
        )
    )
    printed = []

    _run(env_path, print_fn=printed.append)

    written = toml_path.read_text()
    assert "Renamed [sandbox.env] to [sandbox.exe]." in printed
    assert "[sandbox.exe]" in written
    assert "[sandbox.env]" not in written
    assert "# credentials for this provider" in written
    assert read_env(env_path)["EXE_API_URL"] == "https://exe.dev"


def test_foreign_provider_table_is_a_named_gap(tmp_path):
    env_path = tmp_path / ".env"
    printed = []

    rc = _run(
        env_path,
        set_values=("sandbox.other.OTHER_TOKEN=token",),
        print_fn=printed.append,
    )

    assert rc == GAPS_EXIT_CODE
    assert "sandbox.other is a stale provider table" in "\n".join(printed)
    assert "OTHER_TOKEN" not in read_env(env_path)


def test_docker_shape_renders_no_provider_environment(tmp_path):
    env_path = tmp_path / ".env"

    _run(
        env_path,
        provider="docker",
        set_values=("sandbox.docker.LOCAL_DRUKBOX_VALUE=local",),
    )

    assert "LOCAL_DRUKBOX_VALUE" not in read_env(env_path)


def test_integer_sandbox_timeout_round_trips(tmp_path):
    env_path = tmp_path / ".env"

    _run(env_path, provider="docker")
    _run(env_path)

    timeout = _read_toml(tmp_path / "druks.toml")["sandbox"]["timeout"]
    assert timeout == 180
    assert isinstance(timeout, int)


def test_non_string_known_setting_is_refused(tmp_path):
    env_path = tmp_path / ".env"
    _run(env_path, provider="docker")
    toml_path = tmp_path / "druks.toml"
    body = toml_path.read_text().replace('operator_app_id = ""', "operator_app_id = 123")
    toml_path.write_text(body)

    with pytest.raises(ValueError, match="github.operator_app_id must be a string"):
        _run(env_path)


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
        "[sandbox.<provider>]",
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
        set_values=(
            "secrets.webhook_secret=rotated-secret",
            "urls.webhook_host=hooks.example.com",
        ),
        print_fn=printed.append,
    )

    config = _read_toml(tmp_path / "druks.toml")
    values = read_env(env_path)
    assert config["github"]["operator_app_id"] == "101"
    assert config["secrets"]["webhook_secret"] == "rotated-secret"
    assert config["sandbox"]["provider"] == "docker"
    assert "GITHUB_OPERATOR_APP_ID" not in values
    assert "DRUKS_WEBHOOK_SECRET" not in values
    assert values["DRUKS_WEBHOOK_HOST"] == "hooks.example.com"
    assert "docker compose up -d" in "\n".join(printed)


def test_generated_secrets_never_regenerate_on_rerun(tmp_path):
    env_path = tmp_path / ".env"
    _run(env_path)
    first = _read_toml(tmp_path / "druks.toml")["secrets"]

    _run(env_path, set_values=("github.operator_app_id=101",))

    assert _read_toml(tmp_path / "druks.toml")["secrets"] == first


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

    _run(env_path, provider="docker", set_values=("env.EXAMPLE_OPTION=",))

    assert "EXAMPLE_OPTION" not in read_env(env_path)
    assert "EXAMPLE_OPTION=" not in env_path.read_text()


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
    assert "DRUKS_ENDPOINT" not in read_env(env_path)


def test_reserved_sandbox_env_key_is_a_named_gap_and_is_not_rendered(tmp_path):
    env_path = tmp_path / ".env"
    printed = []

    rc = _run(
        env_path,
        provider="exoscale",
        set_values=(
            "sandbox.exoscale.EXOSCALE_API_KEY=key",
            "sandbox.exoscale.DATABASE_URL=wrong",
        ),
        print_fn=printed.append,
    )

    assert rc == GAPS_EXIT_CODE
    assert "sandbox.exoscale.DATABASE_URL is reserved by druks" in "\n".join(printed)
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


def test_operator_edits_survive_a_writer_pass(tmp_path):
    env_path = tmp_path / ".env"
    _run(env_path, provider="docker")
    toml_path = tmp_path / "druks.toml"
    body = toml_path.read_text()
    body = body.replace(
        'operator_app_id = ""',
        '# our app, registered 2026-07 by ops\noperator_app_id = ""\ncustom_label = "blue"',
    )
    body += "\n[operator]\nretries = 4\nenabled = true\n"
    toml_path.write_text(body)

    _run(env_path, set_values=("github.operator_app_id=101",))

    config = _read_toml(toml_path)
    written = toml_path.read_text()
    assert config["github"]["custom_label"] == "blue"
    assert config["operator"] == {"retries": 4, "enabled": True}
    assert "# our app, registered 2026-07 by ops" in written


def test_missing_known_tables_gap_without_rewriting_the_operators_file(tmp_path):
    """Canonicalization fills missing tables in memory for the render; the
    operator's file keeps exactly the shape they wrote."""
    env_path = tmp_path / ".env"
    _run(env_path, provider="docker")
    toml_path = tmp_path / "druks.toml"
    body = toml_path.read_text()
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
    assert "sandbox.provider is empty" in "\n".join(printed)
    assert "github.reviewer_app_id is empty" in "\n".join(printed)
    assert config["github"]["operator_app_id"] == "101"
    assert "GITHUB_OPERATOR_APP_ID" not in read_env(env_path)


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


def test_setup_toml_is_the_settings_source(tmp_path, monkeypatch):
    env_path = tmp_path / ".env"
    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir()
    (secrets_dir / "operator.pem").write_text("operator-pem")
    (secrets_dir / "reviewer.pem").write_text("reviewer-pem")

    rc = _run(
        env_path,
        set_values=(
            "github.operator_app_id=101",
            "github.reviewer_app_id=202",
            "urls.endpoint=https://druks.example",
            "urls.webhook_host=hooks.druks.example",
            "sandbox.image=druks-sandbox:test",
            "sandbox.timeout=240",
            "sandbox.exe.EXE_API_TOKEN=exe-token",
            "sandbox.exe.TAILSCALE_TAILNET=tail.ts.net",
        ),
    )

    assert rc == 0
    toml_path = tmp_path / "druks.toml"
    config = _read_toml(toml_path)
    monkeypatch.setenv("DRUKS_CONFIG", str(toml_path))
    settings = Settings()

    assert settings.identity.model_dump() == config["identity"]
    assert settings.urls.model_dump() == config["urls"]
    assert settings.secrets.webhook_secret == config["secrets"]["webhook_secret"]
    assert settings.secrets.secrets_key == config["secrets"]["secrets_key"]
    assert settings.sandbox.service_token == config["sandbox"]["service_token"]
    assert settings.sandbox.service_url == config["sandbox"]["service_url"]
    assert settings.sandbox.image == config["sandbox"]["image"]
    assert settings.sandbox.timeout == float(config["sandbox"]["timeout"])

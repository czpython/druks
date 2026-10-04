import re
from pathlib import Path

import pytest
from druks import database
from druks.durable import engine as durable_engine
from druks.settings import Settings, ensure_data_dirs
from druks.testing import TEST_DATABASE_URL, make_settings
from pydantic import ValidationError

_SECRETS_KEY = "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA="


def test_auth_defaults_to_none_and_blesses_no_header(tmp_path):
    settings = make_settings(tmp_path)
    assert settings.identity.mode == "none"
    assert not settings.identity.header


@pytest.mark.parametrize("header", ["", "   "])
def test_header_mode_requires_the_operator_to_name_the_header(tmp_path, header):
    with pytest.raises(ValidationError):
        make_settings(tmp_path, identity={"mode": "header", "header": header})


def test_jwt_mode_requires_its_verification_targets(tmp_path):
    complete = {
        "header": "X-Edge-Assertion",
        "jwks_url": "https://edge.example.com/jwks.json",
        "jwt_issuer": "https://edge.example.com",
        "jwt_audience": "druks",
    }
    settings = make_settings(tmp_path, identity={"mode": "jwt", **complete})
    assert settings.identity.jwt_identity_claim == "/email"
    # Each required field, blanked in turn, refuses jwt mode.
    for name in complete:
        with pytest.raises(ValidationError):
            make_settings(
                tmp_path,
                identity={"mode": "jwt", **complete, name: "  "},
            )
    with pytest.raises(ValidationError, match="jwt_identity_claim must be a JSON Pointer"):
        make_settings(tmp_path, identity={"mode": "jwt", **complete, "jwt_identity_claim": "email"})


@pytest.mark.parametrize("mode", ["header", "jwt"])
def test_the_pat_slot_cannot_be_the_identity_header(tmp_path, mode):
    # Authorization always parses bearer-first, so an assertion configured
    # there could never be read — a total lockout, refused at startup.
    with pytest.raises(ValidationError):
        make_settings(
            tmp_path,
            identity={
                "mode": mode,
                "header": "authorization",
                "jwks_url": "https://edge.example.com/jwks.json",
                "jwt_issuer": "https://edge.example.com",
                "jwt_audience": "druks",
            },
        )


def _write_secrets(tmp_path, monkeypatch, secrets):
    """Give Druks its secrets as files, and no secret in the environment."""
    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir()
    for name, value in {"secrets_key": _SECRETS_KEY, **secrets}.items():
        (secrets_dir / name).write_text(value)
    monkeypatch.setenv("DRUKS_SECRETS_DIR", str(secrets_dir))
    for variable in ("DRUKS_SECRETS_KEY", "DRUKS_DATABASE_URL", "DRUKS_REDIS_URL"):
        monkeypatch.delenv(variable)


def test_toml_sets_the_tables_and_the_secrets_directory_sets_the_secrets(tmp_path, monkeypatch):
    config_path = tmp_path / "druks.toml"
    config_path.write_text(
        """
timezone = "Europe/Madrid"
[identity]
mode = "header"
header = "X-Edge-Email"

[urls]
endpoint = "https://druks.example.com"
webhook_host = "hooks.example.com"

[sandbox]
service_url = "https://sandbox.example.com"
image = "sandbox:latest"
timeout = 180
""".strip()
        + "\n"
    )
    monkeypatch.setenv("DRUKS_CONFIG", str(config_path))
    _write_secrets(
        tmp_path,
        monkeypatch,
        {
            "database_url": "postgresql+psycopg://druks:database-password@db/druks",
            "redis_url": "redis://:redis-password@redis:6379/3",
            "sandbox.service_token": "sandbox-token",
        },
    )

    settings = Settings()

    assert settings.identity.mode == "header"
    assert settings.identity.header == "X-Edge-Email"
    assert settings.urls.endpoint == "https://druks.example.com"
    assert settings.urls.webhook_host == "hooks.example.com"
    assert settings.secrets_key.get_secret_value() == _SECRETS_KEY
    assert settings.sandbox.service_token.get_secret_value() == "sandbox-token"
    assert settings.redis_url.get_secret_value() == "redis://:redis-password@redis:6379/3"
    assert settings.sandbox.service_url == "https://sandbox.example.com"
    assert settings.sandbox.image == "sandbox:latest"
    assert settings.sandbox.timeout == 180.0
    assert settings.timezone == "Europe/Madrid"
    for secret in (_SECRETS_KEY, "database-password", "redis-password", "sandbox-token"):
        assert secret not in repr(settings)


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ('secrets_key = "key"', "secrets_key is a secret"),
        ('[sandbox]\nservice_token = "token"', "sandbox.service_token is a secret"),
        ('data_dir = "/home/op/druks-data"', "data_dir is not a druks.toml key"),
    ],
)
def test_druks_toml_refuses_a_secret_and_a_setting_of_the_environment(
    tmp_path, monkeypatch, body, message
):
    config_path = tmp_path / "druks.toml"
    config_path.write_text(body + "\n")
    monkeypatch.setenv("DRUKS_CONFIG", str(config_path))

    with pytest.raises(ValueError, match=message):
        Settings()


def test_the_environment_sets_a_secret_under_its_prefixed_name(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DRUKS_SANDBOX_SERVICE_TOKEN", "environment-token")
    monkeypatch.setenv("DRUKS_TIMEZONE", "Asia/Tokyo")
    # Drukbox reads these names, and they share the environment file of an install.
    monkeypatch.setenv("DATABASE_URL", "sqlite:///drukbox")
    monkeypatch.setenv("SECRETS_KEY", "drukbox-key")

    settings = Settings()

    assert settings.sandbox.service_token.get_secret_value() == "environment-token"
    assert settings.database_url.get_secret_value() == TEST_DATABASE_URL
    assert settings.timezone == "UTC"


def test_a_secret_in_the_environment_and_in_a_file_refuses_construction(tmp_path, monkeypatch):
    _write_secrets(tmp_path, monkeypatch, {})
    monkeypatch.setenv("DRUKS_SECRETS_KEY", _SECRETS_KEY)

    with pytest.raises(ValueError, match="secrets_key is set in the environment and in a secret"):
        Settings()


def test_only_an_explicit_issuer_url_changes_the_mint_base(tmp_path):
    public = {"endpoint": "https://druks.example.com", "webhook_host": "hooks.example.com"}
    assert make_settings(tmp_path, urls=public).sandbox.issuer_url == "http://127.0.0.1:8001"

    settings = make_settings(tmp_path, urls=public, sandbox={"issuer_url": "http://10.0.0.5:8001"})

    assert settings.sandbox.issuer_url == "http://10.0.0.5:8001"


def test_auth_mode_environment_variable_is_ignored(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DRUKS_CONFIG", raising=False)
    monkeypatch.setenv("DRUKS_AUTH_MODE", "header")

    settings = Settings()

    assert settings.identity.mode == "none"


def test_blank_toml_value_uses_submodel_default(tmp_path, monkeypatch):
    config_path = tmp_path / "druks.toml"
    config_path.write_text('[identity]\njwt_identity_claim = ""\n')
    monkeypatch.setenv("DRUKS_CONFIG", str(config_path))

    settings = Settings()

    assert settings.identity.jwt_identity_claim == "/email"


def test_harness_config_root_expands_the_environment_path(tmp_path, monkeypatch):
    monkeypatch.setenv("DRUKS_HARNESS_CONFIG_ROOT", "~/harness-config")

    settings = make_settings(tmp_path)

    assert settings.harness_config_root == Path.home() / "harness-config"


def test_harness_config_root_defaults_to_the_druks_config_directory(tmp_path):
    settings = make_settings(tmp_path)

    assert settings.harness_config_root == Path.home() / ".config/druks/harnesses"


def test_missing_explicit_config_refuses_construction(tmp_path, monkeypatch):
    config_path = tmp_path / "missing.toml"
    monkeypatch.setenv("DRUKS_CONFIG", str(config_path))

    with pytest.raises(ValueError, match=re.escape(str(config_path))):
        Settings()


def test_ensure_data_dirs_provisions_skills_dir(tmp_path):
    # Startup creates skills_dir, or the first skill install's write raises OSError.
    skills_dir = tmp_path / "shared" / "skills"
    settings = make_settings(tmp_path, sandbox_skills_dir=skills_dir)
    ensure_data_dirs(settings)
    assert settings.skills_dir.is_dir()
    assert settings.files_dir.is_dir()


def test_installation_timezone_defaults_to_utc(tmp_path):
    assert make_settings(tmp_path).timezone == "UTC"


@pytest.mark.parametrize("timezone", ["Not/A/Zone", "/etc/passwd", "../UTC"])
def test_installation_timezone_rejects_invalid_zones(tmp_path, timezone):
    with pytest.raises(ValidationError, match="Unknown IANA timezone"):
        make_settings(tmp_path, timezone=timezone)


def test_development_example_pins_the_installation_timezone(tmp_path, monkeypatch):
    example = Path(__file__).resolve().parents[2] / "druks.toml.example"
    config = tmp_path / "druks.toml"
    config.write_text(example.read_text())
    monkeypatch.setenv("DRUKS_CONFIG", str(config))
    monkeypatch.setenv("DRUKS_TIMEZONE", "Asia/Tokyo")
    assert Settings().timezone == "UTC"


def test_pool_settings_reach_the_app_engine_and_dbos(tmp_path, monkeypatch):
    settings = make_settings(
        tmp_path, database_pool_size=3, database_max_overflow=4, dbos_pool_size=5
    )
    monkeypatch.setattr(database, "load_settings", lambda: settings)
    monkeypatch.setattr(durable_engine, "load_settings", lambda: settings)
    monkeypatch.setattr(durable_engine, "_initialized", False)
    configs = []
    monkeypatch.setattr(durable_engine, "DBOS", lambda config: configs.append(config))

    pool = database.create_async_engine_from_url(settings.database_url.get_secret_value()).pool
    durable_engine.init_dbos()

    assert (pool.size(), pool._max_overflow) == (3, 4)
    assert configs[0]["sys_db_pool_size"] == 5

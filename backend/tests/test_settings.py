import re
from pathlib import Path

import pytest
from druks.settings import Settings, ensure_data_dirs
from druks.testing import make_settings
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
    assert settings.identity.jwt_identity_claim == "email"
    # Each required field, blanked in turn, refuses jwt mode.
    for name in complete:
        with pytest.raises(ValidationError):
            make_settings(
                tmp_path,
                identity={"mode": "jwt", **complete, name: "  "},
            )


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


def test_toml_populates_authored_submodels(tmp_path, monkeypatch):
    config_path = tmp_path / "druks.toml"
    config_path.write_text(
        f"""
[identity]
mode = "header"
header = "X-Edge-Email"

[urls]
endpoint = "https://druks.example.com"
webhook_host = "hooks.example.com"

[secrets]
secrets_key = "{_SECRETS_KEY}"

[sandbox]
service_url = "https://sandbox.example.com"
service_token = "sandbox-token"
image = "sandbox:latest"
timeout = 180
""".strip()
        + "\n"
    )
    monkeypatch.setenv("DRUKS_CONFIG", str(config_path))

    settings = Settings()

    assert settings.identity.mode == "header"
    assert settings.identity.header == "X-Edge-Email"
    assert settings.urls.endpoint == "https://druks.example.com"
    assert settings.urls.webhook_host == "hooks.example.com"
    assert settings.secrets.secrets_key == _SECRETS_KEY
    assert settings.sandbox.service_token == "sandbox-token"
    assert settings.sandbox.service_url == "https://sandbox.example.com"
    assert settings.sandbox.image == "sandbox:latest"
    assert settings.sandbox.timeout == 180.0


def test_auth_mode_environment_variable_is_ignored(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DRUKS_CONFIG", raising=False)
    monkeypatch.setenv("DRUKS_AUTH_MODE", "header")

    settings = Settings(secrets={"secrets_key": _SECRETS_KEY})

    assert settings.identity.mode == "none"


def test_blank_toml_value_uses_submodel_default(tmp_path, monkeypatch):
    config_path = tmp_path / "druks.toml"
    config_path.write_text(
        f"""
[identity]
jwt_identity_claim = ""

[secrets]
secrets_key = "{_SECRETS_KEY}"
""".strip()
        + "\n"
    )
    monkeypatch.setenv("DRUKS_CONFIG", str(config_path))

    settings = Settings()

    assert settings.identity.jwt_identity_claim == "email"


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
    # The settings UI installs skill collections into skills_dir; if startup
    # doesn't create it, the first install's write raises OSError → opaque 500.
    # This is the DRUKS_SKILLS_DIR-outside-data_dir case that bit us.
    skills_dir = tmp_path / "shared" / "skills"
    settings = make_settings(tmp_path, sandbox_skills_dir=skills_dir)
    ensure_data_dirs(settings)
    assert settings.skills_dir.is_dir()
    assert settings.files_dir.is_dir()

import pytest
from conftest import make_settings
from druks.settings import Settings, ensure_data_dirs, load_settings
from pydantic import ValidationError


def test_auth_defaults_to_none_and_blesses_no_header(tmp_path, monkeypatch):
    monkeypatch.setenv("DRUKS_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("DRUKS_AUTH_MODE", raising=False)
    monkeypatch.delenv("DRUKS_AUTH_HEADER", raising=False)
    settings = load_settings()
    assert settings.auth_mode == "none"
    assert not settings.auth_header


@pytest.mark.parametrize("auth_header", ["", "   "])
def test_header_mode_requires_the_operator_to_name_the_header(tmp_path, monkeypatch, auth_header):
    monkeypatch.setenv("DRUKS_DATA_DIR", str(tmp_path))
    with pytest.raises(ValidationError):
        Settings(auth_mode="header", auth_header=auth_header)  # type: ignore[call-arg]


def test_jwt_mode_requires_its_verification_targets(tmp_path):
    complete = {
        "auth_header": "X-Edge-Assertion",
        "auth_jwks_url": "https://edge.example.com/jwks.json",
        "auth_jwt_issuer": "https://edge.example.com",
        "auth_jwt_audience": "druks",
    }
    settings = make_settings(tmp_path, auth_mode="jwt", **complete)
    assert settings.auth_jwt_identity_claim == "email"
    # Each required field, blanked in turn, refuses jwt mode.
    for name in complete:
        with pytest.raises(ValidationError):
            make_settings(tmp_path, auth_mode="jwt", **{**complete, name: "  "})


@pytest.mark.parametrize("auth_mode", ["header", "jwt"])
def test_the_pat_slot_cannot_be_the_identity_header(tmp_path, auth_mode):
    # Authorization always parses bearer-first, so an assertion configured
    # there could never be read — a total lockout, refused at startup.
    with pytest.raises(ValidationError):
        make_settings(
            tmp_path,
            auth_mode=auth_mode,
            auth_header="authorization",
            auth_jwks_url="https://edge.example.com/jwks.json",
            auth_jwt_issuer="https://edge.example.com",
            auth_jwt_audience="druks",
        )


def test_ensure_data_dirs_provisions_skills_dir(tmp_path, monkeypatch):
    # The settings UI installs skill collections into skills_dir; if startup
    # doesn't create it, the first install's write raises OSError → opaque 500.
    # This is the DRUKS_SKILLS_DIR-outside-data_dir case that bit us.
    monkeypatch.setenv("DRUKS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DRUKS_SKILLS_DIR", str(tmp_path / "shared" / "skills"))
    settings = load_settings()
    ensure_data_dirs(settings)
    assert settings.skills_dir.is_dir()


def test_settings_no_longer_carries_agent_knob_fields(monkeypatch, tmp_path):

    monkeypatch.setenv("DRUKS_DATA_DIR", str(tmp_path))
    settings = load_settings()

    for forbidden in (
        "codex_model",
        "codex_fast_mode",
        "codex_reasoning_effort",
        "codex_plan_reasoning_effort",
        "codex_evaluate_reasoning_effort",
        "claude_model",
        "claude_fast_mode",
        "claude_reasoning_effort",
        "claude_plan_review_effort",
        "claude_implement_effort",
    ):
        assert forbidden not in Settings.model_fields, (
            f"{forbidden!r} should have moved to user_settings"
        )
        assert not hasattr(settings, forbidden), f"Settings instance still exposes {forbidden!r}"

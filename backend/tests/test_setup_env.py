from pathlib import Path

from druks.setup_env import GAPS_EXIT_CODE, read_env, run_setup


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


def test_fresh_run_writes_template_with_secrets_and_reports_gaps(tmp_path):
    env_path = tmp_path / ".env"

    rc = _run(env_path)

    assert rc == GAPS_EXIT_CODE  # required values are blank on a fresh box
    values = read_env(env_path)
    # Secrets generated, not blank, and drukbox's token list mirrors ours.
    assert len(values["DRUKS_WEBHOOK_SECRET"]) == 64
    assert values["SERVICE_TOKENS"] == values["DRUKS_SANDBOX_SERVICE_TOKEN"]
    # Path defaults rendered against the HOST install dir / home.
    assert values["GITHUB_OPERATOR_PEM"] == "/home/op/druks/secrets/operator.pem"
    assert values["DRUKS_DATA_DIR"] == "/home/op/druks-data"
    # Provider block present.
    assert values["DEFAULT_HOST_PROVIDER"] == "exe"
    assert values["TAILSCALE_ENABLED"] == "true"
    # exe.dev is the identity edge; druks maps its asserted email header.
    assert values["DRUKS_AUTH_MODE"] == "header"
    assert values["DRUKS_AUTH_HEADER"] == "X-ExeDev-Email"
    # The secrets dir is created alongside.
    assert (tmp_path / "secrets").is_dir()


def test_aws_provider_block(tmp_path):
    env_path = tmp_path / ".env"

    _run(env_path, provider="aws")

    values = read_env(env_path)
    assert values["DEFAULT_HOST_PROVIDER"] == "aws"
    # A remote shape reaches the dashboard through its own edge — no localhost
    # default leaks in; the operator supplies the real public URL.
    assert values["DRUKS_ENDPOINT"] == ""
    assert values["TAILSCALE_ENABLED"] == "false"
    assert "AWS_REGION" in values
    # Bring-your-own-edge: header mode, and the operator must name the header
    # their proxy injects — a required gap, never a guessed default.
    assert values["DRUKS_AUTH_MODE"] == "header"
    assert values["DRUKS_AUTH_HEADER"] == ""
    # The sandbox-VM instance profile stays a documented, commented knob.
    assert "# AWS_INSTANCE_PROFILE=" in env_path.read_text()


def test_docker_provider_wires_drukbox_on_host(tmp_path):
    """The provider choice selects the drukbox wiring block, nothing else —
    required values and PEMs gate the boot the same as any provider."""
    env_path = tmp_path / ".env"

    rc = _run(env_path, provider="docker")

    assert rc == GAPS_EXIT_CODE  # same GitHub/PEM gaps as every shape
    values = read_env(env_path)
    assert values["DEFAULT_HOST_PROVIDER"] == "docker"
    # Pointed at drukbox on the host (`make dev`), not a drukbox container.
    assert values["DRUKS_SANDBOX_SERVICE_URL"] == "http://127.0.0.1:8000"
    assert values["DRUKS_SANDBOX_SERVICE_TOKEN"] == "dev-token"
    assert values["DRUKS_SANDBOX_IMAGE"].endswith("druks-sandbox:latest")
    assert "TAILSCALE_TAILNET" not in values and "AWS_REGION" not in values
    # The local dashboard's URL is fixed, so the OAuth-MCP callback base is
    # defaulted — connecting an OAuth MCP server works without a manual edit.
    assert values["DRUKS_ENDPOINT"] == "http://localhost:8001"
    # Loopback dashboard, no edge: identity mode none.
    assert values["DRUKS_AUTH_MODE"] == "none"
    assert "DRUKS_AUTH_HEADER" not in values


def test_rerun_preserves_values_secrets_and_operator_additions(tmp_path):
    env_path = tmp_path / ".env"
    _run(env_path)
    first = read_env(env_path)
    # Operator fills a value and adds a custom var by hand.
    env_path.write_text(
        env_path.read_text().replace("GITHUB_OPERATOR_APP_ID=", "GITHUB_OPERATOR_APP_ID=111")
        + "\nMY_CUSTOM_FLAG=on\n"
    )

    _run(env_path)

    values = read_env(env_path)
    assert values["GITHUB_OPERATOR_APP_ID"] == "111"
    assert values["DRUKS_WEBHOOK_SECRET"] == first["DRUKS_WEBHOOK_SECRET"]  # not regenerated
    assert values["MY_CUSTOM_FLAG"] == "on"  # hand edits survive


def test_rerun_preserves_the_identity_header_choice(tmp_path):
    env_path = tmp_path / ".env"
    _run(env_path)
    env_path.write_text(
        env_path.read_text().replace(
            "DRUKS_AUTH_HEADER=X-ExeDev-Email", "DRUKS_AUTH_HEADER=X-Custom-Email"
        )
    )

    _run(env_path)

    assert read_env(env_path)["DRUKS_AUTH_HEADER"] == "X-Custom-Email"


def test_rerun_keeps_the_provider_the_env_was_written_with(tmp_path):
    env_path = tmp_path / ".env"
    _run(env_path, provider="aws")

    _run(env_path, provider="exe")  # flag ignored on re-run

    assert read_env(env_path)["DEFAULT_HOST_PROVIDER"] == "aws"


def test_interactive_prompts_fill_only_blanks(tmp_path):
    env_path = tmp_path / ".env"
    answers = iter(
        [
            "111",  # operator app id
            "222",  # reviewer app id
            "",  # linear api key — skipped
            "",  # linear webhook secret — skipped
            "",  # druks endpoint — skipped
            "tail.ts.net",  # tailnet
            "",  # ts oauth client id
            "",  # ts oauth client secret
            "exe-token",  # exe api token
        ]
    )

    rc = _run(env_path, interactive=True, input_fn=lambda _prompt: next(answers))

    values = read_env(env_path)
    assert values["GITHUB_OPERATOR_APP_ID"] == "111"
    assert values["GITHUB_REVIEWER_APP_ID"] == "222"
    assert values["EXE_API_TOKEN"] == "exe-token"
    # Required values all present — only the PEM files gate the boot now.
    assert rc == GAPS_EXIT_CODE
    gaps_cleared = _run(env_path, interactive=False)
    assert gaps_cleared == GAPS_EXIT_CODE  # still missing PEM files

    # Drop the PEMs in → boot-ready.
    (tmp_path / "secrets" / "operator.pem").write_text("pem")
    (tmp_path / "secrets" / "reviewer.pem").write_text("pem")
    assert _run(env_path) == 0


def test_rerun_with_complete_env_prompts_nothing(tmp_path):
    env_path = tmp_path / ".env"
    answers = iter(["111", "222", "", "", "", "t.ts.net", "", "", "tok"])
    _run(env_path, interactive=True, input_fn=lambda _p: next(answers))
    (tmp_path / "secrets" / "operator.pem").write_text("pem")
    (tmp_path / "secrets" / "reviewer.pem").write_text("pem")

    def explode(_prompt):
        raise AssertionError("prompted despite complete .env")

    assert _run(env_path, interactive=True, input_fn=explode) == 0


def test_custom_pem_location_is_not_boot_gated(tmp_path):
    """A PEM outside the install dir can't be checked pre-boot — doctor owns
    that post-boot; setup must not block on it."""
    env_path = tmp_path / ".env"
    answers = iter(["111", "222", "", "", "", "t.ts.net", "", "", "tok"])
    _run(env_path, interactive=True, input_fn=lambda _p: next(answers))
    body = env_path.read_text().replace(
        "GITHUB_OPERATOR_PEM=/home/op/druks/secrets/operator.pem",
        "GITHUB_OPERATOR_PEM=/etc/keys/operator.pem",
    )
    env_path.write_text(body)
    (tmp_path / "secrets" / "reviewer.pem").write_text("pem")

    assert _run(env_path) == 0

from pathlib import Path

# The docker image swaps the drukbox entrypoint so Claude can run as ``druks``.
# The swap must still honour the secrets-proxy boot contract: the box sends
# HTTPS through that proxy, and Claude verifies TLS against the installed CA.
ENTRYPOINT = Path(__file__).resolve().parents[2] / "deploy" / "sandbox" / "entrypoint.sh"


def test_sandbox_entrypoint_installs_the_secrets_proxy_ca():
    script = ENTRYPOINT.read_text()

    assert "SECRETS_PROXY_CA" in script
    assert "update-ca-certificates" in script
    assert "/usr/local/share/ca-certificates/drukbox.crt" in script


def test_sandbox_entrypoint_points_git_at_the_github_placeholder():
    script = ENTRYPOINT.read_text()

    assert "GH_TOKEN" in script
    assert "gh auth git-credential" in script

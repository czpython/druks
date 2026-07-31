from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from druks import doctor
from druks.testing import make_settings


def _named(results: list[doctor.CheckResult], name: str) -> doctor.CheckResult:
    return next(result for result in results if result.name == name)


def test_webhook_secret_fails_on_placeholder(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, webhook_secret="change-me")

    result = doctor.check_webhook_secret(settings)

    assert not result.ok
    assert "DRUKS_WEBHOOK_SECRET" in result.detail


def test_webhook_secret_passes_when_set(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, webhook_secret="a-real-secret")

    result = doctor.check_webhook_secret(settings)

    assert result.ok


def test_installations_fails_without_app_creds(tmp_path: Path) -> None:
    # No operator app configured → the client can't even be built; doctor
    # reports the failure instead of raising.
    settings = make_settings(tmp_path)

    result = doctor.check_installations(settings)

    assert not result.ok
    assert "installations" in result.name


def test_installations_lists_accounts(tmp_path: Path, monkeypatch) -> None:
    settings = make_settings(tmp_path)

    class _FakeClient:
        async def list_installation_accounts(self):
            return ("clawhaven",)

    monkeypatch.setattr("druks.doctor.get_github_client", lambda _s: _FakeClient())

    result = doctor.check_installations(settings)

    assert result.ok
    assert "clawhaven" in result.detail


def test_installations_fails_when_app_has_none(tmp_path: Path, monkeypatch) -> None:
    settings = make_settings(tmp_path)

    class _FakeClient:
        async def list_installation_accounts(self):
            return ()

    monkeypatch.setattr("druks.doctor.get_github_client", lambda _s: _FakeClient())

    result = doctor.check_installations(settings)

    assert not result.ok
    assert "no installations" in result.detail


def test_github_app_fails_when_pem_missing(tmp_path: Path) -> None:
    settings = make_settings(
        tmp_path,
        github_operator_app_id="12345",
        github_operator_private_key_path=None,
    )

    result = doctor.check_github_operator_app(settings)

    assert not result.ok
    assert "GITHUB_OPERATOR_PRIVATE_KEY_PATH" in result.detail


def test_github_app_fails_when_pem_does_not_exist(tmp_path: Path) -> None:
    settings = make_settings(
        tmp_path,
        github_operator_app_id="12345",
        github_operator_private_key_path=tmp_path / "missing.pem",
    )

    result = doctor.check_github_operator_app(settings)

    assert not result.ok
    assert "does not exist" in result.detail


def test_github_app_fails_when_pem_is_not_a_key(tmp_path: Path) -> None:
    pem_path = tmp_path / "fake.pem"
    pem_path.write_text("not actually a PEM key\n")
    settings = make_settings(
        tmp_path,
        github_operator_app_id="12345",
        github_operator_private_key_path=pem_path,
    )

    result = doctor.check_github_operator_app(settings)

    assert not result.ok
    assert "PEM" in result.detail


def test_github_app_passes_when_live_mint_succeeds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pem_path = tmp_path / "real.pem"
    pem_path.write_text("-----BEGIN RSA PRIVATE KEY-----\n...\n-----END RSA PRIVATE KEY-----\n")

    async def fake_slug(*, app_id: str, private_key: str) -> str:
        return "druks-operator"

    monkeypatch.setattr(doctor, "_github_app_slug", fake_slug)
    settings = make_settings(
        tmp_path,
        github_operator_app_id="12345",
        github_operator_private_key_path=pem_path,
    )

    result = doctor.check_github_operator_app(settings)

    assert result.ok
    assert "druks-operator" in result.detail


def test_github_app_fails_when_live_mint_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pem_path = tmp_path / "real.pem"
    pem_path.write_text("-----BEGIN RSA PRIVATE KEY-----\n...\n-----END RSA PRIVATE KEY-----\n")

    async def fake_slug(*, app_id: str, private_key: str) -> str:
        raise RuntimeError("401 Unauthorized")

    monkeypatch.setattr(doctor, "_github_app_slug", fake_slug)
    settings = make_settings(
        tmp_path,
        github_operator_app_id="12345",
        github_operator_private_key_path=pem_path,
    )

    result = doctor.check_github_operator_app(settings)

    assert not result.ok
    assert "401" in result.detail


def test_data_dir_fails_when_missing(tmp_path: Path) -> None:
    missing = tmp_path / "absent"
    settings = make_settings(tmp_path, data_dir=missing)

    result = doctor.check_data_dir(settings)

    assert not result.ok
    assert "does not exist" in result.detail


def test_data_dir_passes_when_writable(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, data_dir=tmp_path)

    result = doctor.check_data_dir(settings)

    assert result.ok
    assert not (tmp_path / ".doctor-write-probe").exists()


def test_database_fails_when_unreachable(tmp_path: Path) -> None:
    settings = make_settings(
        tmp_path,
        database_url="postgresql+psycopg://druks:druks@127.0.0.1:1/druks",
    )

    result = doctor.check_database(settings)

    assert not result.ok


def test_redis_fails_on_unreachable_host(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, redis_url="redis://127.0.0.1:1/0")

    result = doctor.check_redis(settings)

    assert not result.ok
    assert "127.0.0.1:1" in result.detail


def test_drukbox_passes_when_unconfigured(tmp_path: Path) -> None:
    """Sandbox URL empty → no drukbox to talk to."""
    settings = make_settings(tmp_path)
    assert settings.sandbox_service_url == ""

    result = doctor.check_drukbox(settings)

    assert result.ok
    assert "not configured" in result.detail


def test_run_checks_covers_all_check_names(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)

    results = doctor.run_checks(settings)

    # Installed extensions contribute their own checks alongside the platform's.
    assert {result.name for result in results} >= {
        "webhook_secret",
        "webhook_ingress",
        "installations",
        "github_operator_app",
        "github_reviewer_app",
        "ship:linear",
        "ship:jira",
        "claude_credentials",
        "codex_credentials",
        "data_dir",
        "database",
        "redis",
        "drukbox",
        "capability_modules",
    }


def test_harness_credentials_fail_when_not_connected(tmp_path: Path) -> None:
    # No credential rows committed => both harnesses read as not connected.
    settings = make_settings(tmp_path)

    result = _named(doctor.check_harness_credentials(settings), "codex_credentials")

    assert not result.ok
    assert "not connected" in result.detail


def test_harness_credential_check_expired() -> None:
    past = datetime.now(UTC) - timedelta(hours=1)
    result = doctor._harness_credential_check("claude", connected=True, expires_at=past)
    assert not result.ok
    assert "expired" in result.detail


def test_harness_credential_check_connected() -> None:
    future = datetime.now(UTC) + timedelta(hours=6)
    result = doctor._harness_credential_check("claude", connected=True, expires_at=future)
    assert result.ok
    assert "connected" in result.detail


def test_webhook_ingress_passes_on_druks_401(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        doctor.httpx,
        "post",
        lambda url, content, timeout: httpx.Response(401),
    )
    settings = make_settings(tmp_path, webhook_host="hooks.example.com")

    result = doctor.check_webhook_ingress(settings)

    assert result.ok


def test_webhook_ingress_fails_on_foreign_404(tmp_path: Path, monkeypatch) -> None:
    """The wildcard-DNS incident: a proxy that doesn't know the host
    answers 404 and the delivery never reaches druks."""
    monkeypatch.setattr(
        doctor.httpx,
        "post",
        lambda url, content, timeout: httpx.Response(404, headers={"server": "nginx"}),
    )
    settings = make_settings(tmp_path, webhook_host="hooks.example.com")

    result = doctor.check_webhook_ingress(settings)

    assert not result.ok
    assert "nginx" in result.detail


def test_print_results_returns_nonzero_on_any_failure(tmp_path: Path, capsys) -> None:
    results = [
        doctor.CheckResult(name="a", ok=True, detail="ok"),
        doctor.CheckResult(name="b", ok=False, detail="broken"),
    ]

    exit_code = doctor.print_results(results)

    assert exit_code == 1
    captured = capsys.readouterr()
    assert "✗" in captured.out
    assert "1 check(s) failed" in captured.out


def test_print_results_returns_zero_when_all_pass(capsys) -> None:
    results = [doctor.CheckResult(name="a", ok=True, detail="ok")]

    exit_code = doctor.print_results(results)

    assert exit_code == 0
    assert "all checks passed" in capsys.readouterr().out


def _fake_sandbox_client(monkeypatch, *, reattach_fails=False):
    """Async-context-manager stubs mirroring acquire/attach/release."""
    import contextlib

    class _FakeExec:
        ok = True
        stderr = ""

    class _FakeSandbox:
        id = "host-doc"

        async def exec(self, argv, timeout):
            return _FakeExec()

    calls = []

    class _FakeClient:
        @contextlib.asynccontextmanager
        async def acquire(self):
            calls.append("acquire")
            yield _FakeSandbox()

        @contextlib.asynccontextmanager
        async def attach(self, *, host_id):
            calls.append(f"attach:{host_id}")
            if reattach_fails:
                raise TimeoutError("dial timed out")
            yield _FakeSandbox()

        async def release(self, *, host_id):
            calls.append(f"release:{host_id}")

    monkeypatch.setattr(doctor, "sandbox_client", _FakeClient())
    return calls


def test_sandbox_e2e_not_configured_is_ok(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, sandbox_service_url="")

    result = doctor.check_sandbox_e2e(settings)

    assert result.ok
    assert result.detail == "not configured"


def test_sandbox_e2e_exercises_dial_and_reattach(tmp_path: Path, monkeypatch) -> None:
    calls = _fake_sandbox_client(monkeypatch)
    settings = make_settings(tmp_path, sandbox_service_url="http://127.0.0.1:8780")

    result = doctor.check_sandbox_e2e(settings)

    assert result.ok
    assert "reattach" in result.detail
    assert calls == ["acquire", "attach:host-doc", "release:host-doc"]


def test_sandbox_e2e_failure_names_the_phase_and_releases(tmp_path: Path, monkeypatch) -> None:
    """A reattach failure is the bug class worth this check — the error
    surfaces in the detail, and the VM must still be released."""
    calls = _fake_sandbox_client(monkeypatch, reattach_fails=True)
    settings = make_settings(tmp_path, sandbox_service_url="http://127.0.0.1:8780")

    result = doctor.check_sandbox_e2e(settings)

    assert not result.ok
    assert "dial timed out" in result.detail
    assert "release:host-doc" in calls


def test_run_checks_includes_sandbox_e2e_only_when_flagged(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, sandbox_service_url="")

    default = {r.name for r in doctor.run_checks(settings)}
    flagged = {r.name for r in doctor.run_checks(settings, sandbox=True)}

    assert "sandbox_e2e" not in default
    assert "sandbox_e2e" in flagged

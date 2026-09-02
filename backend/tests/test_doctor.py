from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from druks import doctor
from druks.database import db_session
from druks.sandbox.exceptions import TemplateNotFound
from druks.services.models import ServiceIdentity
from druks.testing import make_settings


@asynccontextmanager
async def _fixture_check_engine(_settings):
    from druks.database import db_session

    yield db_session().bind


_SECRETS_KEY = "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA="


def _named(results: list[doctor.CheckResult], name: str) -> doctor.CheckResult:
    return next(result for result in results if result.name == name)


@pytest.fixture
def doctor_db(druks_db, monkeypatch: pytest.MonkeyPatch):
    """Point doctor's one-off engines at the test transaction. Doctor's checks
    remove their session from the ambient registry, so rebind the fixture's
    afterwards for the teardown that still needs it."""
    monkeypatch.setattr(doctor, "_check_engine", _fixture_check_engine)
    yield druks_db
    db_session.registry.set(druks_db)


async def _connect_github(slug: str = "druks-operator") -> ServiceIdentity:
    return await ServiceIdentity.connect(
        "github",
        identity={"app_id": "12345", "slug": slug},
        secrets={
            "private_key": "-----BEGIN RSA PRIVATE KEY-----\n...\n-----END RSA PRIVATE KEY-----\n",
            "webhook_secret": "hook-secret",
        },
    )


def _github_identity_result(results: list[doctor.CheckResult]) -> doctor.CheckResult:
    return next(result for result in results if result.name == "github_identity")


async def test_service_identities_pending_when_a_required_service_is_absent(
    tmp_path: Path, doctor_db
) -> None:
    result = _github_identity_result(await doctor.check_service_identities(make_settings(tmp_path)))

    assert not result.ok
    assert result.pending
    assert "not connected" in result.detail


async def test_service_identities_report_the_connected_row(tmp_path: Path, doctor_db) -> None:
    await _connect_github()

    result = _github_identity_result(await doctor.check_service_identities(make_settings(tmp_path)))

    assert result.ok
    assert "app_id=12345" in result.detail
    assert "slug=druks-operator" in result.detail


async def test_installations_pending_without_a_connected_identity(
    tmp_path: Path, doctor_db
) -> None:
    # No github row → the zero-argument client can't even be built; doctor
    # reports it as pending operator setup instead of raising.
    result = await doctor.check_installations(make_settings(tmp_path))

    assert not result.ok
    assert result.pending
    assert "installations" in result.name
    assert "not connected" in result.detail


async def test_installations_lists_accounts(tmp_path: Path, doctor_db, monkeypatch) -> None:
    class _FakeClient:
        async def list_installation_accounts(self):
            return ("clawhaven",)

    async def _fake_client():
        return _FakeClient()

    monkeypatch.setattr("druks.doctor.get_github_client", _fake_client)

    result = await doctor.check_installations(make_settings(tmp_path))

    assert result.ok
    assert "clawhaven" in result.detail


async def test_installations_pending_when_app_has_none(
    tmp_path: Path, doctor_db, monkeypatch
) -> None:
    class _FakeClient:
        async def list_installation_accounts(self):
            return ()

    async def _fake_client():
        return _FakeClient()

    monkeypatch.setattr("druks.doctor.get_github_client", _fake_client)

    result = await doctor.check_installations(make_settings(tmp_path))

    assert not result.ok
    assert result.pending
    assert "no installations" in result.detail


async def test_installations_builds_the_client_from_the_row(
    tmp_path: Path, doctor_db, monkeypatch
) -> None:
    # The real zero-argument factory resolves the row inside doctor's own
    # bound session; the fake transport keeps GitHub out of it.
    await _connect_github()

    class _FakeClient:
        async def list_installation_accounts(self):
            return ("clawhaven",)

    real_factory = doctor.get_github_client
    built: list[str] = []

    async def _tracking_factory():
        client = await real_factory()
        built.append(client._app_id)
        return _FakeClient()

    monkeypatch.setattr(doctor, "get_github_client", _tracking_factory)

    result = await doctor.check_installations(make_settings(tmp_path))

    assert result.ok
    assert built == ["12345"]


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


async def test_drukbox_passes_when_unconfigured(tmp_path: Path) -> None:
    """Sandbox URL empty → no drukbox to talk to."""
    settings = make_settings(tmp_path)
    assert settings.sandbox.service_url == ""

    result = await doctor.check_drukbox(settings)

    assert result.ok
    assert "not configured" in result.detail


async def test_declared_sandboxes_pass_when_none_are_declared(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_templates = AsyncMock()
    monkeypatch.setattr(doctor, "prepare_sandbox_templates", request_templates)
    monkeypatch.setattr(doctor, "get_declared_sandboxes", lambda: {})
    settings = make_settings(tmp_path, sandbox={"service_url": "http://drukbox"})

    result = await doctor.check_declared_sandboxes(settings)

    assert result == doctor.CheckResult(
        name="sandbox_templates",
        ok=True,
        detail="no declared sandboxes",
    )
    request_templates.assert_awaited_once_with()


@pytest.mark.parametrize(
    ("status", "ok", "pending"),
    [
        ("available", True, False),
        ("building", False, True),
        ("failed", False, False),
    ],
)
async def test_declared_sandboxes_report_template_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: str,
    ok: bool,
    pending: bool,
) -> None:
    declared = {"requirements-1": SimpleNamespace(setup="sandboxes/setup.sh")}
    request_templates = AsyncMock()
    lookup = AsyncMock(return_value=SimpleNamespace(status=status))
    monkeypatch.setattr(doctor, "prepare_sandbox_templates", request_templates)
    monkeypatch.setattr(doctor, "get_declared_sandboxes", lambda: declared)
    monkeypatch.setattr(
        doctor,
        "sandbox_client",
        SimpleNamespace(get_template=lookup),
    )
    settings = make_settings(tmp_path, sandbox={"service_url": "http://drukbox"})

    results = await doctor.check_declared_sandboxes(settings)

    request_templates.assert_awaited_once_with()
    lookup.assert_awaited_once_with(setup_script_hash="requirements-1")
    assert len(results) == 1
    assert results[0].ok is ok
    assert results[0].pending is pending
    assert "sandboxes/setup.sh" in results[0].detail
    assert "requirements-1" in results[0].detail
    assert status in results[0].detail


async def test_declared_sandboxes_report_missing_template(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(doctor, "prepare_sandbox_templates", AsyncMock())
    monkeypatch.setattr(
        doctor,
        "get_declared_sandboxes",
        lambda: {"requirements-1": SimpleNamespace(setup="sandboxes/setup.sh")},
    )
    monkeypatch.setattr(
        doctor,
        "sandbox_client",
        SimpleNamespace(get_template=AsyncMock(side_effect=TemplateNotFound("missing"))),
    )
    settings = make_settings(tmp_path, sandbox={"service_url": "http://drukbox"})

    result = (await doctor.check_declared_sandboxes(settings))[0]

    assert not result.ok
    assert not result.pending
    assert "sandboxes/setup.sh" in result.detail
    assert "requirements-1" in result.detail
    assert "missing" in result.detail


async def test_run_checks_covers_all_check_names(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)

    results = await doctor.run_checks(settings)

    # Installed apps contribute their own checks alongside the platform's.
    assert {result.name for result in results} >= {
        "webhook_ingress",
        "github_identity",
        "installations",
        "software_factory:settings",
        "review:settings",
        "claude_credentials",
        "codex_credentials",
        "data_dir",
        "database",
        "redis",
        "drukbox",
        "capability_modules",
    }


def test_harness_credentials_pending_when_not_connected(tmp_path: Path) -> None:
    # No credential rows committed => both harnesses read as not connected.
    settings = make_settings(tmp_path)

    result = _named(doctor.check_harness_credentials(settings), "codex_credentials")

    assert not result.ok
    assert result.pending
    assert "not connected" in result.detail


def test_harness_credential_check_expired() -> None:
    # An expired token is a genuine fault (runs would fail), not pending setup.
    past = datetime.now(UTC) - timedelta(hours=1)
    result = doctor._harness_credential_check("claude", connected=True, expires_at=past)
    assert not result.ok
    assert not result.pending
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
    settings = make_settings(tmp_path, urls={"webhook_host": "hooks.example.com"})

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
    settings = make_settings(tmp_path, urls={"webhook_host": "hooks.example.com"})

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


def test_print_results_pending_does_not_fail(capsys) -> None:
    # A healthy fresh box: everything green except unconnected harnesses, which
    # are pending operator setup — the command still exits 0.
    results = [
        doctor.CheckResult(name="database", ok=True, detail="reachable"),
        doctor.CheckResult(
            name="claude_credentials", ok=False, pending=True, detail="not connected"
        ),
    ]

    exit_code = doctor.print_results(results)

    assert exit_code == 0
    captured = capsys.readouterr()
    assert "○" in captured.out
    assert "1 item(s) pending operator setup" in captured.out


def test_print_results_fails_on_a_genuine_fault_alongside_pending(capsys) -> None:
    results = [
        doctor.CheckResult(name="database", ok=False, detail="unreachable"),
        doctor.CheckResult(
            name="claude_credentials", ok=False, pending=True, detail="not connected"
        ),
    ]

    exit_code = doctor.print_results(results)

    assert exit_code == 1
    captured = capsys.readouterr()
    assert "1 check(s) failed" in captured.out
    assert "1 pending" in captured.out


def _fake_sandbox_client(
    monkeypatch: pytest.MonkeyPatch,
    *,
    reattach_fails: bool = False,
    missing_command: str | None = None,
) -> list[object]:
    """Async-context-manager stubs mirroring acquire/attach/release."""

    class _FakeSandbox:
        id = "host-doc"

        def __init__(self, connection: str) -> None:
            self.connection = connection

        async def exec(self, argv: list[str], timeout: float) -> SimpleNamespace:
            calls.append((self.connection, argv, timeout))
            command_missing = missing_command is not None and argv == [
                "sh",
                "-c",
                f"command -v {missing_command}",
            ]
            return SimpleNamespace(
                ok=not command_missing,
                stdout="" if command_missing else "/usr/bin/tool\n",
                stderr="missing\n" if command_missing else "",
            )

    calls: list[object] = []

    class _FakeClient:
        @asynccontextmanager
        async def acquire(self):
            calls.append("acquire")
            yield _FakeSandbox("acquire")

        @asynccontextmanager
        async def attach(self, *, host_id):
            calls.append(f"attach:{host_id}")
            if reattach_fails:
                raise TimeoutError("dial timed out")
            yield _FakeSandbox("reattach")

        async def release(self, *, host_id):
            calls.append(f"release:{host_id}")

    monkeypatch.setattr(doctor, "sandbox_client", _FakeClient())
    return calls


async def test_sandbox_e2e_not_configured_is_ok(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, sandbox={"service_url": ""})

    result = await doctor.check_sandbox_e2e(settings)

    assert result.ok
    assert result.detail == "not configured"


async def test_sandbox_e2e_exercises_dial_and_reattach(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _fake_sandbox_client(monkeypatch)
    settings = make_settings(tmp_path, sandbox={"service_url": "http://127.0.0.1:8780"})

    results = await doctor.check_sandbox_e2e(settings)

    assert [result.name for result in results] == [
        "sandbox_e2e",
        "sandbox_binary:claude",
        "sandbox_binary:codex",
        "sandbox_binary:opencode",
    ]
    assert all(result.ok for result in results)
    assert calls == [
        "acquire",
        ("acquire", ["echo", "doctor"], 30.0),
        ("acquire", ["sh", "-c", "command -v claude"], 30.0),
        ("acquire", ["sh", "-c", "command -v codex"], 30.0),
        ("acquire", ["sh", "-c", "command -v opencode"], 30.0),
        "attach:host-doc",
        ("reattach", ["echo", "doctor"], 30.0),
        "release:host-doc",
    ]


async def test_sandbox_e2e_reports_a_missing_harness_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _fake_sandbox_client(monkeypatch, missing_command="codex")
    settings = make_settings(tmp_path, sandbox={"service_url": "http://127.0.0.1:8780"})

    results = await doctor.check_sandbox_e2e(settings)

    assert _named(results, "sandbox_binary:claude").ok
    assert not _named(results, "sandbox_binary:codex").ok
    assert ("acquire", ["sh", "-c", "command -v codex"], 30.0) in calls


async def test_sandbox_e2e_failure_names_the_phase_and_releases(
    tmp_path: Path, monkeypatch
) -> None:
    """A reattach failure is the bug class worth this check — the error
    surfaces in the detail, and the VM must still be released."""
    calls = _fake_sandbox_client(monkeypatch, reattach_fails=True)
    settings = make_settings(tmp_path, sandbox={"service_url": "http://127.0.0.1:8780"})

    result = await doctor.check_sandbox_e2e(settings)

    assert not result.ok
    assert "dial timed out" in result.detail
    assert "release:host-doc" in calls


async def test_run_checks_includes_sandbox_e2e_only_when_flagged(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, sandbox={"service_url": ""})

    default = {r.name for r in await doctor.run_checks(settings)}
    flagged = {r.name for r in await doctor.run_checks(settings, sandbox=True)}

    assert "sandbox_e2e" not in default
    assert "sandbox_e2e" in flagged

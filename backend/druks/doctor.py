import asyncio
import importlib
import pkgutil
import socket
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from urllib.parse import urlparse

import httpx
from drukbox_sdk import SandboxAPI
from githubkit import AppAuthStrategy, GitHub
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from .agents import Agent
from .core.apis.github import get_github_client
from .database import create_engine_from_url
from .extensions.loader import iter_extensions
from .extensions.registry import _ROLES, agents, autodiscover, webhooks, workflows
from .harnesses.models import HarnessConnection
from .harnesses.registry import get_harnesses
from .sandbox.client import sandbox_client
from .settings import Settings, load_settings
from .webhooks.base import Webhook
from .workflows import Workflow


@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool
    detail: str


def check_webhook_secret(settings: Settings) -> CheckResult:
    secret = settings.webhook_secret
    if not secret or secret == "change-me":
        return CheckResult(
            name="webhook_secret",
            ok=False,
            detail="DRUKS_WEBHOOK_SECRET is empty or the placeholder.",
        )
    return CheckResult(name="webhook_secret", ok=True, detail="set")


def check_installations(settings: Settings) -> CheckResult:
    """Where druks may act = the operator App's installation accounts;
    this check is the audit surface for that set."""
    try:
        accounts = asyncio.run(get_github_client(settings).list_installation_accounts())
    except Exception as exc:  # noqa: BLE001 — doctor reports, never raises
        return CheckResult(
            name="installations",
            ok=False,
            detail=f"could not list operator App installations: {exc}",
        )
    if not accounts:
        return CheckResult(
            name="installations",
            ok=False,
            detail="operator App has no installations — install it on your org/user",
        )
    return CheckResult(
        name="installations",
        ok=True,
        detail=f"operator App installed on: {', '.join(accounts)}",
    )


def check_github_operator_app(settings: Settings) -> CheckResult:
    return _check_github_app(
        name="github_operator_app",
        app_id=settings.github_operator_app_id,
        pem_path=settings.github_operator_private_key_path,
        env_id="GITHUB_OPERATOR_APP_ID",
        env_pem="GITHUB_OPERATOR_PRIVATE_KEY_PATH",
    )


def check_github_reviewer_app(settings: Settings) -> CheckResult:
    return _check_github_app(
        name="github_reviewer_app",
        app_id=settings.github_reviewer_app_id,
        pem_path=settings.github_reviewer_private_key_path,
        env_id="GITHUB_REVIEWER_APP_ID",
        env_pem="GITHUB_REVIEWER_PRIVATE_KEY_PATH",
    )


def _check_github_app(
    *,
    name: str,
    app_id: str | None,
    pem_path: Path | None,
    env_id: str,
    env_pem: str,
) -> CheckResult:
    if not app_id:
        return CheckResult(name=name, ok=False, detail=f"{env_id} is unset.")
    if not pem_path:
        return CheckResult(name=name, ok=False, detail=f"{env_pem} is unset.")
    if not pem_path.exists():
        return CheckResult(name=name, ok=False, detail=f"{pem_path} does not exist.")
    body = pem_path.read_text(errors="replace")
    if "BEGIN RSA PRIVATE KEY" not in body and "BEGIN PRIVATE KEY" not in body:
        return CheckResult(name=name, ok=False, detail=f"{pem_path} is not a PEM private key.")
    # Live-mint a JWT and ask the GitHub API who we are. Proves the App ID
    # matches the PEM and the App still exists.
    try:
        slug = asyncio.run(_github_app_slug(app_id=app_id, private_key=body))
    except Exception as error:  # noqa: BLE001 — auth failures surface as fail
        return CheckResult(name=name, ok=False, detail=f"GitHub auth failed: {error}")
    return CheckResult(name=name, ok=True, detail=f"app_id={app_id} slug={slug}")


async def _github_app_slug(*, app_id: str, private_key: str) -> str:
    # ``async with`` lazily creates the underlying httpx client on entry
    # and closes it on exit; a bare ``GitHub(...)`` has no client to close.
    async with GitHub(AppAuthStrategy(app_id, private_key)) as gh:
        response = await gh.rest.apps.async_get_authenticated()
        return response.parsed_data.slug


def check_linear(settings: Settings) -> CheckResult:
    if not settings.linear_api_key:
        return CheckResult(name="linear", ok=True, detail="not configured")
    if not settings.linear_webhook_secret:
        return CheckResult(
            name="linear",
            ok=False,
            detail="LINEAR_API_KEY set but LINEAR_WEBHOOK_SECRET empty.",
        )
    return CheckResult(name="linear", ok=True, detail="set")


def check_jira(settings: Settings) -> CheckResult:
    if not (settings.jira_base_url and settings.jira_email and settings.jira_api_token):
        return CheckResult(name="jira", ok=True, detail="not configured")
    if not settings.jira_webhook_secret:
        return CheckResult(
            name="jira",
            ok=False,
            detail="Jira configured but JIRA_WEBHOOK_SECRET empty.",
        )
    return CheckResult(name="jira", ok=True, detail="set")


def _harness_credential_check(
    name: str, *, connected: bool, expires_at: datetime | None
) -> CheckResult:
    check_name = f"{name}_credentials"
    if not connected:
        return CheckResult(
            check_name, ok=False, detail=f"not connected — connect {name} in Settings."
        )
    if expires_at and expires_at <= datetime.now(UTC):
        return CheckResult(
            check_name,
            ok=False,
            detail=f"token expired {expires_at.isoformat()} — reconnect {name}.",
        )
    detail = f"connected; token expires {expires_at.isoformat()}" if expires_at else "connected"
    return CheckResult(check_name, ok=True, detail=detail)


def check_harness_credentials(settings: Settings) -> list[CheckResult]:
    # One result per registered harness, so a newly-registered one is covered
    # without editing doctor. Credentials live in the DB: this reports the row's
    # presence + expiry, not a host file. A plain session reads it directly —
    # doctor is a one-off, so it never binds the ambient db_session registry.
    engine = create_engine_from_url(settings.database_url)
    try:
        with Session(engine) as session:
            results: list[CheckResult] = []
            for harness in get_harnesses():
                row = session.scalar(
                    select(HarnessConnection).where(
                        HarnessConnection.harness == harness.name, HarnessConnection.is_default
                    )
                )
                results.append(
                    _harness_credential_check(
                        harness.name,
                        connected=bool(row),
                        expires_at=row.expires_at if row else None,
                    )
                )
            return results
    except Exception as error:  # noqa: BLE001 — a DB-read failure is one fail, not a doctor crash
        return [
            CheckResult(
                name="harness_credentials", ok=False, detail=f"cannot read credentials: {error}"
            )
        ]
    finally:
        engine.dispose()


def check_webhook_ingress(settings: Settings) -> CheckResult:
    """An unsigned probe POST must come back 401 — druks itself rejecting
    it proves the path DNS → TLS → edge → druks works. Anything else means
    the request died in front of druks (wrong DNS record, foreign proxy)."""
    host = settings.webhook_host
    if not host:
        return CheckResult(name="webhook_ingress", ok=True, detail="not configured")
    url = f"https://{host}/_external/github/events/"
    try:
        response = httpx.post(url, content=b"{}", timeout=10.0)
    except Exception as error:  # noqa: BLE001 — DNS/connect/TLS failures surface as fail
        return CheckResult(name="webhook_ingress", ok=False, detail=f"POST {url}: {error}")
    if response.status_code == 401:
        return CheckResult(name="webhook_ingress", ok=True, detail=f"{url} reaches druks")
    server = response.headers.get("server", "?")
    return CheckResult(
        name="webhook_ingress",
        ok=False,
        detail=f"POST {url} got HTTP {response.status_code} from server={server} — not reaching druks.",  # noqa: E501
    )


def check_data_dir(settings: Settings) -> CheckResult:
    data_dir = settings.data_dir
    if not data_dir.exists():
        return CheckResult(name="data_dir", ok=False, detail=f"{data_dir} does not exist.")
    probe = data_dir / ".doctor-write-probe"
    try:
        probe.write_text("ok")
        probe.unlink()
    except OSError as error:
        return CheckResult(name="data_dir", ok=False, detail=f"{data_dir} not writable: {error}")
    return CheckResult(name="data_dir", ok=True, detail=str(data_dir))


def check_database(settings: Settings) -> CheckResult:
    try:
        engine = create_engine_from_url(settings.database_url)
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        engine.dispose()
    except Exception as error:  # noqa: BLE001 — any DB error surfaces as a fail
        return CheckResult(
            name="database",
            ok=False,
            detail=f"connecting to Postgres failed (is it up + migrated?): {error}",
        )
    return CheckResult(name="database", ok=True, detail="reachable")


def check_drukbox(settings: Settings) -> CheckResult:
    if not settings.sandbox_service_url:
        return CheckResult(name="drukbox", ok=True, detail="not configured")
    try:
        report = asyncio.run(_drukbox_doctor(settings))
    except Exception as error:  # noqa: BLE001 — surface any SDK/transport failure as fail
        return CheckResult(name="drukbox", ok=False, detail=f"unreachable: {error}")
    if report.ok:
        return CheckResult(name="drukbox", ok=True, detail=f"{report.active_provider} ok")
    fail = next(c for c in report.checks if c.status != "ok")
    detail = f"{fail.name}: {fail.detail}"
    if fail.hint:
        detail += f" ({fail.hint})"
    return CheckResult(name="drukbox", ok=False, detail=detail)


async def _drukbox_doctor(settings: Settings):
    api = SandboxAPI(
        base_url=settings.sandbox_service_url,
        token=settings.sandbox_service_token,
        timeout=settings.sandbox_service_timeout,
    )
    try:
        return await api.doctor()
    finally:
        await api.aclose()


def check_sandbox_e2e(settings: Settings) -> CheckResult:
    """Provision a real VM and exercise the two dial paths builds use:
    the acquire-time connection and a reattach from a GET-built record.
    Costs one VM-minute — opt-in via ``druks doctor --sandbox``, never
    part of the default check set."""
    if not settings.sandbox_service_url:
        return CheckResult(name="sandbox_e2e", ok=True, detail="not configured")
    try:
        detail = asyncio.run(_sandbox_e2e())
    except Exception as error:  # noqa: BLE001 — doctor reports, never raises
        return CheckResult(name="sandbox_e2e", ok=False, detail=f"{error}")
    return CheckResult(name="sandbox_e2e", ok=True, detail=detail)


async def _sandbox_e2e() -> str:
    start = time.monotonic()
    # acquire rolls its own host back on failure; once it yields, we own
    # the release.
    async with sandbox_client.acquire() as sandbox:
        host_id = sandbox.id
        try:
            await _doctor_exec(sandbox)
            provision_seconds = time.monotonic() - start

            reattach_start = time.monotonic()
            async with sandbox_client.attach(host_id=host_id) as reattached:
                await _doctor_exec(reattached)
            reattach_seconds = time.monotonic() - reattach_start
        finally:
            await sandbox_client.release(host_id=host_id)
    return (
        f"provision+dial {provision_seconds:.0f}s · "
        f"reattach {reattach_seconds:.1f}s · host {host_id}"
    )


async def _doctor_exec(sandbox) -> None:
    result = await sandbox.exec(["echo", "doctor"], timeout=30.0)
    if not result.ok:
        raise RuntimeError(result.stderr)


def check_redis(settings: Settings) -> CheckResult:
    parsed = urlparse(settings.redis_url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 6379
    try:
        with socket.create_connection((host, port), timeout=2):
            pass
    except OSError as error:
        return CheckResult(name="redis", ok=False, detail=f"{host}:{port} unreachable: {error}")
    return CheckResult(name="redis", ok=True, detail=f"{host}:{port}")


def _defined_capability(module: ModuleType) -> tuple[str, str] | None:
    """The capability a leaf module DEFINES itself (not merely imports), as
    ``(role, registry_key)``, or None. The key is how ``autodiscover``'s import side
    effect records it — comparing against the keys discovery already registered
    tells a stray (never imported) from one a canonical role module re-exports
    (imported transitively, so registered fine)."""
    name = module.__name__
    for value in vars(module).values():
        if isinstance(value, type) and issubclass(value, Workflow) and value.__module__ == name:
            return "workflows", value.kind
        if (
            isinstance(value, type)
            and issubclass(value, Webhook)
            and not value.abstract
            and value.__module__ == name
        ):
            return "webhooks", f"{value.__module__}.{value.__qualname__}"
        if isinstance(value, Agent) and value.module == name:
            return "agents", value.name
    return None


def check_capability_modules(settings: Settings) -> CheckResult:
    """A capability self-registers as an import side effect, but ``autodiscover``
    only imports leaf modules named for their role. A capability under any other
    filename (the natural singular ``webhook.py``, say) silently never registers —
    catch that by running the real discovery, then importing each off-canon leaf and
    flagging any whose capability the discovery walk didn't already register."""
    by_role = {"workflows": workflows, "webhooks": webhooks, "agents": agents}
    packages = [extension.package for extension in iter_extensions()]
    strays: list[str] = []
    for package in packages:
        # The canonical walk first, then snapshot what it registered — so a
        # capability a role module re-exports counts as discovered, and importing
        # an off-canon module below (which self-registers too) can't mask a stray.
        autodiscover(package)
        discovered = {role: set(registry._items) for role, registry in by_role.items()}
        pkg = importlib.import_module(package)
        for info in pkgutil.walk_packages(pkg.__path__, prefix=f"{package}."):
            if info.ispkg or info.name.rsplit(".", 1)[-1] in _ROLES:
                continue
            try:
                module = importlib.import_module(info.name)
            except Exception as exc:  # noqa: BLE001 — doctor reports, never raises
                strays.append(f"{info.name}: failed to import — {exc}")
                continue
            found = _defined_capability(module)
            if not found:
                continue
            role, key = found
            if key not in discovered[role]:
                strays.append(
                    f"{info.name} defines a {role} but won't be discovered — rename to {role}.py"
                )
    return CheckResult(
        name="capability_modules",
        ok=not strays,
        detail="; ".join(strays) or "all capability files discoverable",
    )


def check_extensions(settings: Settings) -> list[CheckResult]:
    """Each installed extension's own checks, namespaced under it. Read off the class
    app-lessly through the loader, so doctor never imports an extension's private
    modules. A check that raises when run is contained under the extension's name by
    ``_run_extension_check``, and the core checks are separate ``CHECKS`` entries a
    broken extension can't hide."""
    results: list[CheckResult] = []
    for extension in iter_extensions():
        for check in extension.checks or ():
            results.append(_run_extension_check(extension.name, check, settings))
    return results


def _run_extension_check(extension_name: str, check, settings: Settings) -> CheckResult:
    """One extension check, its result namespaced under the extension. A check that
    raises, or returns anything but a ``CheckResult`` (a missing ``return`` yields
    ``None``), becomes a failing result rather than escaping and hiding later checks."""
    label = getattr(check, "__name__", repr(check))
    try:
        outcome = check(settings)
        if not isinstance(outcome, CheckResult):
            raise TypeError(f"check returned {type(outcome).__name__}, expected CheckResult")
    except Exception as error:  # noqa: BLE001 — the check fails, never aborts
        return CheckResult(
            name=f"{extension_name}:{label}", ok=False, detail=f"check raised: {error}"
        )
    return CheckResult(
        name=f"{extension_name}:{outcome.name}", ok=outcome.ok, detail=outcome.detail
    )


CHECKS = (
    check_webhook_secret,
    check_webhook_ingress,
    check_installations,
    check_github_operator_app,
    check_github_reviewer_app,
    check_linear,
    check_jira,
    check_harness_credentials,
    check_data_dir,
    check_database,
    check_redis,
    check_drukbox,
    check_capability_modules,
    check_extensions,
)


def run_checks(settings: Settings, *, sandbox: bool = False) -> list[CheckResult]:
    # A check yields one result, or several (check_harness_credentials fans out
    # over the harness registry).
    results: list[CheckResult] = []
    for check in CHECKS:
        outcome = check(settings)
        results.extend(outcome if isinstance(outcome, list) else [outcome])
    if sandbox:
        results.append(check_sandbox_e2e(settings))
    return results


def print_results(results: list[CheckResult]) -> int:
    failures = 0
    for result in results:
        glyph = "✓" if result.ok else "✗"
        print(f"{glyph}  {result.name:24s}  {result.detail}")
        if not result.ok:
            failures += 1
    print()
    if failures:
        print(f"doctor: {failures} check(s) failed.")
        return 1
    print("doctor: all checks passed.")
    return 0


def main(*, sandbox: bool = False) -> int:
    try:
        settings = load_settings()
    except Exception as error:  # noqa: BLE001 — Settings can raise any validator error
        print(f"✗  load_settings           {error}")
        print()
        print("doctor: could not load Settings. Fix .env and re-run.")
        return 1
    return print_results(run_checks(settings, sandbox=sandbox))

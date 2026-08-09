import asyncio
import importlib
import os
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
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from .agents import Agent
from .core.apis.github import GITHUB, get_github_client
from .database import create_engine_from_url, db_session
from .extensions.loader import iter_extensions
from .extensions.registry import _ROLES, agents, autodiscover, webhooks, workflows
from .harnesses.models import HarnessConnection
from .harnesses.registry import get_harnesses
from .sandbox.client import sandbox_client
from .service_identities.exceptions import ServiceNotConnectedError
from .service_identities.models import ServiceIdentity
from .settings import Settings, load_settings
from .user_settings.models import UserSettings
from .webhooks.base import Webhook
from .workflows import Workflow


@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool
    detail: str
    # A not-ok check the operator clears in the dashboard, not the shell (an
    # unconnected harness, an uninstalled App). Expected on a fresh box, so it
    # prints but never drives the exit code — only a genuine fault does.
    pending: bool = False


def check_github_identity(settings: Settings) -> CheckResult:
    """The GitHub service identity is database-backed like a harness
    connection: this reports the row's presence, not a file or setting."""
    engine = create_engine_from_url(settings.database_url)
    try:
        with Session(engine) as session:
            db_session.registry.set(session)
            row = ServiceIdentity.get(GITHUB)
    except ServiceNotConnectedError:
        return CheckResult(
            name="github_identity",
            ok=False,
            pending=True,
            detail="not connected — connect GitHub in Settings → Harnesses.",
        )
    except Exception as error:  # noqa: BLE001 — a DB-read failure is a fail, not a crash
        return CheckResult(
            name="github_identity", ok=False, detail=f"cannot read the identity: {error}"
        )
    finally:
        db_session.remove()
        engine.dispose()
    return CheckResult(
        name="github_identity",
        ok=True,
        detail=f"connected; app_id={row.identity['app_id']} slug={row.identity['slug']}",
    )


def check_installations(settings: Settings) -> CheckResult:
    """Where druks may act = the operator App's installation accounts;
    this check is the audit surface for that set. The zero-argument client
    factory reads the service-identity row, so a one-off Session is bound
    into the ambient ``db_session`` registry for the duration."""
    engine = create_engine_from_url(settings.database_url)
    try:
        with Session(engine) as session:
            db_session.registry.set(session)
            client = get_github_client()
        accounts = asyncio.run(client.list_installation_accounts())
    except ServiceNotConnectedError:
        return CheckResult(
            name="installations",
            ok=False,
            pending=True,
            detail="github is not connected — connect it in Settings → Harnesses.",
        )
    except Exception as exc:  # noqa: BLE001 — doctor reports, never raises
        return CheckResult(
            name="installations",
            ok=False,
            detail=f"could not list operator App installations: {exc}",
        )
    finally:
        db_session.remove()
        engine.dispose()
    if not accounts:
        return CheckResult(
            name="installations",
            ok=False,
            pending=True,
            detail="operator App has no installations — install it on your org/user",
        )
    return CheckResult(
        name="installations",
        ok=True,
        detail=f"operator App installed on: {', '.join(accounts)}",
    )


def _harness_credential_check(
    name: str, *, connected: bool, expires_at: datetime | None
) -> CheckResult:
    check_name = f"{name}_credentials"
    if not connected:
        return CheckResult(
            check_name,
            ok=False,
            pending=True,
            detail=f"not connected — connect {name} in Settings.",
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
            fallback_id = session.scalar(
                select(UserSettings.fallback_account_id).where(
                    UserSettings.id == UserSettings.SINGLETON_ID
                )
            )
            results: list[CheckResult] = []
            for harness in get_harnesses():
                row = session.scalar(
                    select(HarnessConnection).where(
                        HarnessConnection.harness == harness.name,
                        HarnessConnection.account_id == fallback_id,
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
    host = settings.urls.webhook_host
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
    if not settings.sandbox.service_url:
        return CheckResult(
            name="drukbox",
            ok=True,
            detail="not configured (deployments: [sandbox].service_url in druks.toml)",
        )
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
        base_url=settings.sandbox.service_url,
        token=settings.sandbox.service_token,
        timeout=settings.sandbox.timeout,
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
    if not settings.sandbox.service_url:
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
    """Each installed extension's resolved settings and own checks, namespaced under it.
    Read off the class app-lessly through the loader, so doctor never imports an
    extension's private modules. A check or settings clean that raises is contained
    under the extension's name, and core checks remain separate ``CHECKS`` entries."""
    engine = create_engine_from_url(settings.database_url)
    try:
        with Session(engine) as session:
            db_session.registry.set(session)
            results: list[CheckResult] = []
            for extension in iter_extensions():
                if settings_model := extension.settings_model:
                    try:
                        problems = extension.settings().clean()
                        detail = "; ".join(
                            f"{settings_model.model_fields[field].title or field}: {message}"
                            for field, message in problems.items()
                        )
                    except Exception as error:  # noqa: BLE001 — settings fail, doctor continues
                        results.append(
                            CheckResult(
                                name=f"{extension.name}:settings",
                                ok=False,
                                detail=f"check raised: {error}",
                            )
                        )
                    else:
                        results.append(
                            CheckResult(
                                name=f"{extension.name}:settings",
                                ok=not problems,
                                detail=detail or "coherent",
                            )
                        )
                for check in extension.checks or ():
                    results.append(_run_extension_check(extension.name, check))
            return results
    finally:
        db_session.remove()
        engine.dispose()


def _run_extension_check(extension_name: str, check) -> CheckResult:
    """One extension check, its result namespaced under the extension. A check that
    raises, or returns anything but a ``CheckResult`` (a missing ``return`` yields
    ``None``), becomes a failing result rather than escaping and hiding later checks."""
    label = getattr(check, "__name__", repr(check))
    try:
        outcome = check()
        if not isinstance(outcome, CheckResult):
            raise TypeError(f"check returned {type(outcome).__name__}, expected CheckResult")
    except Exception as error:  # noqa: BLE001 — the check fails, never aborts
        return CheckResult(
            name=f"{extension_name}:{label}", ok=False, detail=f"check raised: {error}"
        )
    return CheckResult(
        name=f"{extension_name}:{outcome.name}",
        ok=outcome.ok,
        detail=outcome.detail,
        pending=outcome.pending,
    )


CHECKS = (
    check_webhook_ingress,
    check_github_identity,
    check_installations,
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
    pending = 0
    for result in results:
        glyph = "✓" if result.ok else "○" if result.pending else "✗"
        print(f"{glyph}  {result.name:24s}  {result.detail}")
        if result.ok:
            continue
        if result.pending:
            pending += 1
        else:
            failures += 1
    print()
    pending_note = f" ({pending} pending operator setup)" if pending else ""
    if failures:
        print(f"doctor: {failures} check(s) failed{pending_note}.")
        return 1
    if pending:
        print(f"doctor: healthy; {pending} item(s) pending operator setup.")
        return 0
    print("doctor: all checks passed.")
    return 0


def main(*, sandbox: bool = False) -> int:
    config_path = os.environ.get("DRUKS_CONFIG")
    config_source = config_path or ("./druks.toml" if Path("druks.toml").exists() else "env only")
    print(f"doctor: config source: {config_source}")
    try:
        settings = load_settings()
    except Exception as error:  # noqa: BLE001 — Settings can raise any validator error
        print(f"✗  load_settings           {error}")
        print()
        print("doctor: could not load Settings. Fix the configuration and re-run.")
        return 1
    return print_results(run_checks(settings, sandbox=sandbox))

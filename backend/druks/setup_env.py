import base64
import copy
import math
import os
import re
import secrets
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, time
from pathlib import Path
from typing import TypeGuard, cast

GAPS_EXIT_CODE = 3
MIGRATION_EXIT_CODE = 4

_COMPOSE_ENV_KEYS = (
    "DRUKS_UID",
    "DRUKS_GID",
    "DRUKS_TAG",
    "DRUKS_WEB_BIND_HOST",
    "COMPOSE_PROFILES",
)
_ENV_KEY_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_TOML_KEY_PATTERN = re.compile(r"[A-Za-z0-9_-]+")


@dataclass(frozen=True)
class Entry:
    key: str
    source: tuple[str, ...] = ()
    fixed: str = ""
    is_required: bool = False
    prompt: str = ""
    is_install_path: bool = False
    is_remote_only: bool = False


@dataclass(frozen=True)
class Section:
    title: str
    entries: tuple[Entry, ...]


_SECTIONS = (
    Section(
        "GITHUB AND TICKETING",
        (
            Entry(
                "GITHUB_OPERATOR_APP_ID",
                ("github", "operator_app_id"),
                is_required=True,
                prompt="Operator GitHub App id (or run install.sh --apps later)",
            ),
            Entry(
                "GITHUB_OPERATOR_PRIVATE_KEY_PATH",
                ("github", "operator_pem"),
                is_install_path=True,
            ),
            Entry("GITHUB_OPERATOR_PEM", ("github", "operator_pem"), is_install_path=True),
            Entry(
                "GITHUB_REVIEWER_APP_ID",
                ("github", "reviewer_app_id"),
                is_required=True,
                prompt="Reviewer GitHub App id (or run install.sh --apps later)",
            ),
            Entry(
                "GITHUB_REVIEWER_PRIVATE_KEY_PATH",
                ("github", "reviewer_pem"),
                is_install_path=True,
            ),
            Entry("GITHUB_REVIEWER_PEM", ("github", "reviewer_pem"), is_install_path=True),
            Entry(
                "LINEAR_API_KEY",
                ("ticketing", "linear_api_key"),
                prompt="Linear API key (enter to skip Linear)",
            ),
            Entry(
                "LINEAR_WEBHOOK_SECRET",
                ("ticketing", "linear_webhook_secret"),
                prompt="Linear webhook secret (enter to skip)",
            ),
            Entry(
                "DRUKS_ENDPOINT",
                ("urls", "endpoint"),
                prompt="Base URL the operator's browser reaches druks at (enter to skip)",
            ),
        ),
    ),
    Section(
        "GENERATED SECRETS",
        (
            Entry(
                "DRUKS_POSTGRES_PASSWORD",
                ("secrets", "postgres_password"),
                is_required=True,
            ),
            Entry("DRUKS_WEBHOOK_SECRET", ("secrets", "webhook_secret"), is_required=True),
            Entry("DRUKS_SECRETS_KEY", ("secrets", "secrets_key"), is_required=True),
        ),
    ),
    Section(
        "DEPLOYMENT DEFAULTS",
        (
            Entry("DRUKS_DATA_DIR", ("paths", "data_dir")),
            Entry("DRUKS_UPSTREAM", fixed="127.0.0.1:8001"),
            Entry("DRUKS_CLAUDE_HOME", ("paths", "claude_home")),
            Entry("DRUKS_CODEX_HOME", ("paths", "codex_home")),
            Entry("DRUKS_CLAUDE_JSON", ("paths", "claude_json")),
            Entry("DRUKS_WEBHOOK_HOST", ("urls", "webhook_host")),
            Entry("DATABASE_URL", fixed="sqlite+aiosqlite:////data/drukbox.db"),
            Entry("REDIS_URL", fixed="redis://127.0.0.1:6379/2"),
        ),
    ),
    Section(
        "IDENTITY",
        (
            Entry("DRUKS_AUTH_MODE", ("identity", "mode"), is_required=True),
            Entry("DRUKS_AUTH_HEADER", ("identity", "header")),
            Entry("DRUKS_AUTH_JWKS_URL", ("identity", "jwks_url")),
            Entry("DRUKS_AUTH_JWT_ISSUER", ("identity", "jwt_issuer")),
            Entry("DRUKS_AUTH_JWT_AUDIENCE", ("identity", "jwt_audience")),
            Entry(
                "DRUKS_AUTH_JWT_IDENTITY_CLAIM",
                ("identity", "jwt_identity_claim"),
            ),
        ),
    ),
    Section(
        "SANDBOX",
        (
            Entry("DEFAULT_HOST_PROVIDER", ("sandbox", "provider"), is_required=True),
            Entry(
                "DRUKS_SANDBOX_SERVICE_URL",
                ("sandbox", "service_url"),
                is_required=True,
            ),
            Entry(
                "DRUKS_SANDBOX_SERVICE_TOKEN",
                ("secrets", "sandbox_service_token"),
                is_required=True,
            ),
            Entry(
                "SERVICE_TOKENS",
                ("sandbox", "service_tokens"),
                is_remote_only=True,
            ),
            Entry("DRUKS_SANDBOX_IMAGE", ("sandbox", "image")),
        ),
    ),
)

_OWNED_ENV_KEYS = frozenset(entry.key for section in _SECTIONS for entry in section.entries)
_KNOWN_TOML_KEYS = {
    "identity": (
        "mode",
        "header",
        "jwks_url",
        "jwt_issuer",
        "jwt_audience",
        "jwt_identity_claim",
    ),
    "github": ("operator_app_id", "reviewer_app_id", "operator_pem", "reviewer_pem"),
    "ticketing": ("linear_api_key", "linear_webhook_secret"),
    "urls": ("endpoint", "webhook_host"),
    "secrets": (
        "postgres_password",
        "webhook_secret",
        "secrets_key",
        "sandbox_service_token",
    ),
    "paths": ("data_dir", "claude_home", "codex_home", "claude_json"),
    "sandbox": ("provider", "service_url", "image", "service_tokens"),
    "env": (),
}
_TOML_TABLE_COMMENTS = {
    "identity": '# Browser identity: "header", "jwt", or "none".',
    "github": "# GitHub Apps may also be provisioned with install.sh --apps.",
    "ticketing": "# Optional ticketing integration.",
    "urls": "# Public dashboard and webhook ingress addresses.",
    "secrets": "# Generated on first write. Do not regenerate a deployed secret.",
    "paths": "# Host paths. Relative PEM paths are resolved against the install directory.",
    "sandbox": "# Any drukbox provider name; docker and exe select install shapes.",
    "env": "# Additional deployment settings; keys render verbatim unless owned above.",
}


def _hex_secret() -> str:
    return secrets.token_hex(32)


def _secrets_key() -> str:
    return base64.b64encode(secrets.token_bytes(32)).decode()


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if path.exists():
        for line in path.read_text().splitlines():
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            values[key.strip()] = value
    return values


def run_setup(
    env_path: Path,
    *,
    provider: str,
    install_dir: str,
    home: str,
    interactive: bool,
    set_values: tuple[str, ...] = (),
    input_fn: Callable[[str], str] = input,
    print_fn: Callable[[str], None] = print,
) -> int:
    toml_path = env_path.parent / "druks.toml"
    if env_path.exists() and not toml_path.exists():
        _print_migration_recipe(print_fn, env_path, toml_path)
        return MIGRATION_EXIT_CODE

    is_fresh = not toml_path.exists()
    if is_fresh:
        provider = provider.strip()
        if interactive:
            answer = input_fn(f"Sandbox provider [{provider}]: ").strip()
            provider = answer or provider
        if not provider:
            raise ValueError("sandbox provider cannot be blank")
        config = _fresh_config(provider=provider, home=home)
    else:
        config = _read_toml(toml_path)

    is_changed = is_fresh
    for assignment in set_values:
        path, value = _parse_assignment(assignment)
        _set_value(config, path, value)
        is_changed = True

    provider = _get_string(config, ("sandbox", "provider"))
    print_fn(_shape_message(provider))

    if interactive:
        is_changed = (
            _prompt_values(
                config,
                is_fresh=is_fresh,
                input_fn=input_fn,
            )
            or is_changed
        )

    if is_changed:
        _write_toml(toml_path, config)
        config = _read_toml(toml_path)
    if is_fresh:
        _write_gitignore(env_path.parent / ".gitignore")
        (env_path.parent / "secrets").mkdir(mode=0o700, exist_ok=True)

    existing_env = env_path.read_text() if env_path.exists() else ""
    extras = {key: value for key, value in read_env(env_path).items() if key in _COMPOSE_ENV_KEYS}
    env_text = _render_env(config, install_dir=install_dir, extras=extras)
    _write_secure_text(env_path, env_text)

    if existing_env and existing_env != env_text:
        print_fn("Rendered .env changed. Apply it with: docker compose up -d")

    gaps = _collect_gaps(config, env_path=env_path, install_dir=install_dir)
    _print_outcome(print_fn, env_path=env_path, provider=provider, gaps=gaps)
    return GAPS_EXIT_CODE if gaps else 0


def _fresh_config(*, provider: str, home: str) -> dict[str, object]:
    if provider == "docker":
        identity_mode = "none"
        identity_header = ""
        endpoint = "http://localhost:8001"
        service_url = "http://127.0.0.1:8000"
        sandbox_token = "dev-token"
        sandbox_image = "ghcr.io/czpython/druks-sandbox:latest"
    elif provider == "exe":
        identity_mode = "header"
        identity_header = "X-ExeDev-Email"
        endpoint = ""
        service_url = "http://127.0.0.1:8780"
        sandbox_token = _hex_secret()
        sandbox_image = ""
    else:
        identity_mode = "header"
        identity_header = ""
        endpoint = ""
        service_url = "http://127.0.0.1:8780"
        sandbox_token = _hex_secret()
        sandbox_image = ""

    sandbox_env: dict[str, object] = {}
    if provider == "exe":
        sandbox_env = {
            "EXE_API_TOKEN": "",
            "TAILSCALE_TAILNET": "",
            "TAILSCALE_OAUTH_CLIENT_ID": "",
            "TAILSCALE_OAUTH_CLIENT_SECRET": "",
            "EXE_API_URL": "https://exe.dev",
            "EXE_DEFAULT_IMAGE": "ghcr.io/boldsoftware/exeuntu:latest",
            "TAILSCALE_ENABLED": "true",
            "TAILSCALE_API_TIMEOUT": "30",
            "TAILSCALE_AUTH_TAGS": "tag:sandbox",
        }

    return {
        "identity": {
            "mode": identity_mode,
            "header": identity_header,
            "jwks_url": "",
            "jwt_issuer": "",
            "jwt_audience": "",
            "jwt_identity_claim": "email",
        },
        "github": {
            "operator_app_id": "",
            "reviewer_app_id": "",
            "operator_pem": "secrets/operator.pem",
            "reviewer_pem": "secrets/reviewer.pem",
        },
        "ticketing": {
            "linear_api_key": "",
            "linear_webhook_secret": "",
        },
        "urls": {
            "endpoint": endpoint,
            "webhook_host": "",
        },
        "secrets": {
            "postgres_password": _hex_secret(),
            "webhook_secret": _hex_secret(),
            "secrets_key": _secrets_key(),
            "sandbox_service_token": sandbox_token,
        },
        "paths": {
            "data_dir": f"{home.rstrip('/')}/druks-data",
            "claude_home": f"{home.rstrip('/')}/.claude",
            "codex_home": f"{home.rstrip('/')}/.codex",
            "claude_json": f"{home.rstrip('/')}/.claude.json",
        },
        "sandbox": {
            "provider": provider,
            "service_url": service_url,
            "image": sandbox_image,
            "service_tokens": "",
            "env": sandbox_env,
        },
        "env": {},
    }


def _read_toml(path: Path) -> dict[str, object]:
    with path.open("rb") as config_file:
        config = tomllib.load(config_file)
    return _canonical_config(config)


def _validate_config(config: dict[str, object]) -> None:
    for table_name, keys in _KNOWN_TOML_KEYS.items():
        value = config.get(table_name, {})
        if not isinstance(value, dict):
            raise ValueError(f"druks.toml: [{table_name}] must be a table")
        for key in keys:
            configured = value.get(key, "")
            if not isinstance(configured, str):
                raise ValueError(f"druks.toml: {table_name}.{key} must be a string")

    sandbox = config.get("sandbox", {})
    if not isinstance(sandbox, dict):
        raise ValueError("druks.toml: [sandbox] must be a table")
    sandbox_env = sandbox.get("env", {})
    if not isinstance(sandbox_env, dict):
        raise ValueError("druks.toml: [sandbox.env] must be a table")
    for key, value in sandbox_env.items():
        if not isinstance(value, str):
            raise ValueError(f"druks.toml: sandbox.env.{key} must be a string")

    deployment_env = config.get("env", {})
    if not isinstance(deployment_env, dict):
        raise ValueError("druks.toml: [env] must be a table")
    for key, value in deployment_env.items():
        if not isinstance(value, str):
            raise ValueError(f"druks.toml: env.{key} must be a string")


def _canonical_config(config: dict[str, object]) -> dict[str, object]:
    _validate_config(config)
    canonical = copy.deepcopy(config)
    for table_name, keys in _KNOWN_TOML_KEYS.items():
        table = cast(dict[str, object], canonical.setdefault(table_name, {}))
        for key in keys:
            table.setdefault(key, "")
    sandbox = cast(dict[str, object], canonical["sandbox"])
    sandbox.setdefault("env", {})
    if not sandbox["service_tokens"]:
        del sandbox["service_tokens"]

    identity = cast(dict[str, object], canonical["identity"])
    if identity["mode"] != "jwt":
        for key in ("jwks_url", "jwt_issuer", "jwt_audience", "jwt_identity_claim"):
            if not identity[key] or key == "jwt_identity_claim" and identity[key] == "email":
                del identity[key]
    return canonical


def _get_string(config: dict[str, object], path: tuple[str, ...]) -> str:
    current: object = config
    for part in path:
        if not isinstance(current, dict):
            raise ValueError(f"druks.toml: {'.'.join(path)} crosses a non-table value")
        current = current.get(part, "")
    if not isinstance(current, str):
        raise ValueError(f"druks.toml: {'.'.join(path)} must be a string")
    return current


def _set_value(config: dict[str, object], path: tuple[str, ...], value: str) -> None:
    current = config
    for part in path[:-1]:
        child = current.setdefault(part, {})
        if not isinstance(child, dict):
            raise ValueError(f"druks.toml: {'.'.join(path)} crosses a non-table value")
        current = child
    current[path[-1]] = value


def _parse_assignment(assignment: str) -> tuple[tuple[str, ...], str]:
    path_text, separator, value = assignment.partition("=")
    path = tuple(path_text.split("."))
    if not separator or len(path) < 2 or any(not part for part in path):
        raise ValueError(f"invalid --set {assignment!r}; expected key.path=value")
    return path, value


def _prompt_values(
    config: dict[str, object],
    *,
    is_fresh: bool,
    input_fn: Callable[[str], str],
) -> bool:
    prompts = [
        (entry.source, entry.prompt, entry.is_required)
        for section in _SECTIONS
        for entry in section.entries
        if entry.source and entry.prompt
    ]
    identity_mode = _get_string(config, ("identity", "mode"))
    if identity_mode in {"header", "jwt"}:
        prompts.append(
            (
                ("identity", "header"),
                "Identity header your edge injects (e.g. X-Forwarded-Email)",
                True,
            )
        )
    if identity_mode == "jwt":
        prompts.extend(
            (
                (("identity", "jwks_url"), "Identity edge JWKS URL", True),
                (("identity", "jwt_issuer"), "Identity token issuer", True),
                (("identity", "jwt_audience"), "Identity token audience", True),
                (("identity", "jwt_identity_claim"), "Identity claim containing the email", True),
            )
        )

    if _get_string(config, ("sandbox", "provider")) == "exe":
        prompts.extend(
            (
                (("sandbox", "env", "EXE_API_TOKEN"), "exe.dev API token", True),
                (
                    ("sandbox", "env", "TAILSCALE_TAILNET"),
                    'Tailscale magic-DNS suffix (e.g. "yourtail.ts.net")',
                    True,
                ),
                (
                    ("sandbox", "env", "TAILSCALE_OAUTH_CLIENT_ID"),
                    "Tailscale OAuth client id",
                    False,
                ),
                (
                    ("sandbox", "env", "TAILSCALE_OAUTH_CLIENT_SECRET"),
                    "Tailscale OAuth client secret",
                    False,
                ),
            )
        )

    is_changed = False
    for path, prompt, is_required in prompts:
        if _get_string(config, path) or (not is_required and not is_fresh):
            continue
        answer = input_fn(f"{prompt}: ").strip()
        if answer:
            _set_value(config, path, answer)
            is_changed = True
    return is_changed


def _write_toml(path: Path, config: dict[str, object]) -> None:
    canonical = _canonical_config(config)
    text = _render_toml(canonical)
    _write_secure_text(path, text)
    parsed = _read_toml(path)
    if parsed != canonical:
        raise ValueError("druks.toml write verification failed: parsed values changed")


def _render_toml(config: dict[str, object]) -> str:
    lines = [
        "# druks.toml — the deployment. Edit this file, then re-run the installer",
        "# to render and apply it. `druks setup` alone re-renders .env but does",
        "# not restart services.",
        "",
    ]

    root_values = {
        key: value
        for key, value in config.items()
        if not isinstance(value, dict) and not _is_table_array(value)
    }
    if root_values:
        lines.append("# OPERATOR ADDITIONS")
        for key, value in root_values.items():
            lines.append(f"{_toml_key(key)} = {_toml_value(value)}")
        lines.append("")

    for table_name, known_keys in _KNOWN_TOML_KEYS.items():
        lines.append(_TOML_TABLE_COMMENTS[table_name])
        lines.append(f"[{table_name}]")
        table = cast(dict[str, object], config[table_name])
        for key in known_keys:
            if key in table:
                lines.append(f"{key} = {_toml_value(table[key])}")
            elif table_name == "identity":
                lines.append(f'# {key} = "{"email" if key == "jwt_identity_claim" else ""}"')
            else:
                lines.append(f'# {key} = ""')
        additions = {
            key: value
            for key, value in table.items()
            if key not in known_keys
            and key != "env"
            and not isinstance(value, dict)
            and not _is_table_array(value)
        }
        if additions:
            if known_keys:
                lines.append("# OPERATOR ADDITIONS")
            for key, value in additions.items():
                lines.append(f"{_toml_key(key)} = {_toml_value(value)}")
        lines.append("")

        if table_name == "sandbox":
            provider = table["provider"]
            if provider == "docker":
                lines.extend(
                    (
                        "# Host-run drukbox reads its own checkout's environment.",
                        "# Values in this table are preserved but are not rendered to .env.",
                    )
                )
            elif provider == "exe":
                lines.append("# Drukbox environment passed through to the remote stack.")
            else:
                lines.extend(
                    (
                        "# Drukbox environment passed through to the remote stack.",
                        "# Configuration reference: https://github.com/czpython/drukbox",
                        "# See docs/deploy.md in that repository.",
                    )
                )
            lines.append("[sandbox.env]")
            sandbox_env = cast(dict[str, object], table["env"])
            for key, value in sandbox_env.items():
                lines.append(f"{_toml_key(key)} = {_toml_value(value)}")
            lines.append("")

    unknown_tables: list[tuple[tuple[str, ...], dict[str, object], bool]] = []
    for key, value in config.items():
        if isinstance(value, dict) and key not in _KNOWN_TOML_KEYS:
            unknown_tables.append(((key,), value, False))
        elif _is_table_array(value):
            for table in value:
                unknown_tables.append(((key,), table, True))
    for table_name in _KNOWN_TOML_KEYS:
        table = cast(dict[str, object], config[table_name])
        for key, value in table.items():
            if isinstance(value, dict) and not (table_name == "sandbox" and key == "env"):
                unknown_tables.append(((table_name, key), value, False))
            elif _is_table_array(value):
                for item in value:
                    unknown_tables.append(((table_name, key), item, True))

    if unknown_tables:
        lines.extend(("# " + "=" * 60, "# OPERATOR ADDITIONS", "# " + "=" * 60, ""))
        for path, table, is_array in unknown_tables:
            lines.extend(_render_unknown_table(path, table, is_array=is_array))

    return "\n".join(lines).rstrip() + "\n"


def _render_unknown_table(
    path: tuple[str, ...],
    table: dict[str, object],
    *,
    is_array: bool,
) -> list[str]:
    path_text = ".".join(_toml_key(part) for part in path)
    brackets = "[[" if is_array else "["
    closing_brackets = "]]" if is_array else "]"
    lines = [f"{brackets}{path_text}{closing_brackets}"]
    nested: list[tuple[str, dict[str, object]]] = []
    arrays: list[tuple[str, list[dict[str, object]]]] = []
    for key, value in table.items():
        if isinstance(value, dict):
            nested.append((key, value))
        elif _is_table_array(value):
            arrays.append((key, value))
        else:
            lines.append(f"{_toml_key(key)} = {_toml_value(value)}")
    lines.append("")
    for key, value in nested:
        lines.extend(_render_unknown_table((*path, key), value, is_array=False))
    for key, values in arrays:
        for value in values:
            lines.extend(_render_unknown_table((*path, key), value, is_array=True))
    return lines


def _is_table_array(value: object) -> TypeGuard[list[dict[str, object]]]:
    return isinstance(value, list) and bool(value) and all(isinstance(item, dict) for item in value)


def _toml_key(value: str) -> str:
    if _TOML_KEY_PATTERN.fullmatch(value):
        return value
    return f'"{_escape_toml_string(value)}"'


def _toml_value(value: object) -> str:
    if isinstance(value, str):
        return f'"{_escape_toml_string(value)}"'
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("druks.toml: non-finite floats are unsupported")
        return repr(value)
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    if isinstance(value, dict):
        items = (f"{_toml_key(key)} = {_toml_value(item)}" for key, item in value.items())
        return "{ " + ", ".join(items) + " }"
    raise ValueError(f"druks.toml: unsupported operator value {value!r}")


def _escape_toml_string(value: str) -> str:
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError("druks.toml values may not contain newlines or control characters")
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _render_env(
    config: dict[str, object],
    *,
    install_dir: str,
    extras: dict[str, str],
) -> str:
    provider = _get_string(config, ("sandbox", "provider"))
    lines: list[str] = []
    for section in _SECTIONS:
        section_lines: list[str] = []
        for entry in section.entries:
            if entry.is_remote_only and provider == "docker":
                continue
            value = entry.fixed or _get_string(config, entry.source)
            if entry.key == "SERVICE_TOKENS" and not value:
                value = _get_string(config, ("secrets", "sandbox_service_token"))
            if entry.is_install_path and value:
                value = _resolve_install_path(value, install_dir)
            if not value:
                continue
            section_lines.append(_env_line(entry.key, value))
        if section_lines:
            lines.extend(("# " + "=" * 60, f"# {section.title}", "# " + "=" * 60, ""))
            lines.extend(section_lines)
            lines.append("")

    sandbox = cast(dict[str, object], config["sandbox"])
    sandbox_env = cast(dict[str, str], sandbox["env"])
    if provider != "docker":
        pass_through = []
        for key, value in sandbox_env.items():
            if _is_reserved_env_key(key) or not value:
                continue
            if not _ENV_KEY_PATTERN.fullmatch(key):
                raise ValueError(f"druks.toml: sandbox.env key {key!r} is not an env name")
            pass_through.append(_env_line(key, value))
        if pass_through:
            lines.extend(
                (
                    "# " + "=" * 60,
                    "# SANDBOX ENVIRONMENT",
                    "# " + "=" * 60,
                    "",
                    *pass_through,
                    "",
                )
            )

    deployment_env = cast(dict[str, str], config["env"])
    additions = []
    for key, value in deployment_env.items():
        if key in _OWNED_ENV_KEYS or not value:
            continue
        if not _ENV_KEY_PATTERN.fullmatch(key):
            raise ValueError(f"druks.toml: env key {key!r} is not an env name")
        additions.append(_env_line(key, value))
    if additions:
        lines.extend(
            (
                "# " + "=" * 60,
                "# DEPLOYMENT ENVIRONMENT ADDITIONS",
                "# " + "=" * 60,
                "",
                *additions,
                "",
            )
        )

    compose_extras = {key: value for key, value in extras.items() if key not in deployment_env}
    if compose_extras:
        lines.extend(
            (
                "# " + "=" * 60,
                "# OPERATOR ADDITIONS (preserved verbatim by druks setup)",
                "# " + "=" * 60,
                "",
            )
        )
        for key in _COMPOSE_ENV_KEYS:
            if key in compose_extras:
                lines.append(_env_line(key, compose_extras[key]))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _env_line(key: str, value: str) -> str:
    if "\n" in value or "\r" in value:
        raise ValueError(f".env value for {key} may not contain a newline")
    return f"{key}={value}"


def _resolve_install_path(value: str, install_dir: str) -> str:
    path = Path(value)
    if path.is_absolute():
        return str(path)
    return str(Path(install_dir.rstrip("/")) / path)


def _is_reserved_env_key(key: str) -> bool:
    return key.startswith("DRUKS_") or key in _OWNED_ENV_KEYS


def _collect_gaps(
    config: dict[str, object],
    *,
    env_path: Path,
    install_dir: str,
) -> list[str]:
    gaps = [
        f"{entry.key} is empty"
        for section in _SECTIONS
        for entry in section.entries
        if entry.is_required and not _get_string(config, entry.source)
    ]

    identity_mode = _get_string(config, ("identity", "mode"))
    if identity_mode not in {"none", "header", "jwt"}:
        gaps.append("identity.mode must be one of: header, jwt, none")
    if identity_mode in {"header", "jwt"} and not _get_string(config, ("identity", "header")):
        gaps.append("DRUKS_AUTH_HEADER is empty")
    if identity_mode == "jwt":
        for path, env_key in (
            (("identity", "jwks_url"), "DRUKS_AUTH_JWKS_URL"),
            (("identity", "jwt_issuer"), "DRUKS_AUTH_JWT_ISSUER"),
            (("identity", "jwt_audience"), "DRUKS_AUTH_JWT_AUDIENCE"),
            (("identity", "jwt_identity_claim"), "DRUKS_AUTH_JWT_IDENTITY_CLAIM"),
        ):
            if not _get_string(config, path):
                gaps.append(f"{env_key} is empty")

    provider = _get_string(config, ("sandbox", "provider"))
    sandbox = cast(dict[str, object], config["sandbox"])
    sandbox_env = cast(dict[str, str], sandbox["env"])
    for key in sandbox_env:
        if _is_reserved_env_key(key):
            gaps.append(f"sandbox.env.{key} is reserved by druks")

    deployment_env = cast(dict[str, str], config["env"])
    for key in deployment_env:
        if key in _OWNED_ENV_KEYS:
            gaps.append(f"env.{key} is reserved by druks")

    if provider == "exe":
        for key in ("EXE_API_TOKEN", "TAILSCALE_TAILNET"):
            if not _get_string(config, ("sandbox", "env", key)):
                gaps.append(f"{key} is empty")
    elif provider != "docker" and not any(
        value for key, value in sandbox_env.items() if not _is_reserved_env_key(key)
    ):
        gaps.append(f"[sandbox.env] has no configured values for remote provider {provider!r}")

    for key in ("operator_pem", "reviewer_pem"):
        configured = _get_string(config, ("github", key))
        if not configured:
            gaps.append(f"github.{key} is empty")
            continue
        host_path = Path(_resolve_install_path(configured, install_dir))
        install_path = Path(install_dir.rstrip("/"))
        if host_path.is_relative_to(install_path):
            local_path = env_path.parent / host_path.relative_to(install_path)
        elif Path(configured).is_absolute():
            continue
        else:
            local_path = env_path.parent / configured
        if not local_path.is_file():
            gaps.append(f"{host_path} is missing (upload the PEM, or run install.sh --apps)")
    return gaps


def _write_secure_text(path: Path, text: str) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w") as output:
            descriptor = -1
            output.write(text)
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _write_gitignore(path: Path) -> None:
    existing = path.read_text().splitlines() if path.exists() else []
    missing = [entry for entry in ("druks.toml", ".env", "secrets/") if entry not in existing]
    if missing:
        body = "\n".join((*existing, *missing)).lstrip("\n") + "\n"
        path.write_text(body)


def _shape_message(provider: str) -> str:
    if not provider:
        return "sandbox provider is not configured"
    if provider == "docker":
        return "provider 'docker' → local shape; host-run drukbox validates its configuration"
    if provider == "exe":
        return "provider 'exe' → exe shape; check `druks doctor` after boot"
    return (
        f"provider {provider!r} → remote shape; drukbox validates the name — "
        "check `druks doctor` after boot"
    )


def _print_migration_recipe(
    print_fn: Callable[[str], None],
    env_path: Path,
    toml_path: Path,
) -> None:
    print_fn(f"Refusing to replace existing {env_path}: {toml_path} does not exist.")
    print_fn("Back up .env and hand-write druks.toml, copying values verbatim:")
    print_fn("  - DRUKS_POSTGRES_PASSWORD → [secrets].postgres_password")
    print_fn("  - DRUKS_SECRETS_KEY → [secrets].secrets_key")
    print_fn("  - DRUKS_WEBHOOK_SECRET → [secrets].webhook_secret")
    print_fn("  - DRUKS_SANDBOX_SERVICE_TOKEN → [secrets].sandbox_service_token")
    print_fn("  - GITHUB_*_APP_ID → [github]")
    print_fn("  - provider and its credentials → [sandbox] and [sandbox.env]")
    print_fn("  - other hand-added .env keys → [env]")
    print_fn("Nothing was written.")


def _print_outcome(
    print_fn: Callable[[str], None],
    *,
    env_path: Path,
    provider: str,
    gaps: list[str],
) -> None:
    if gaps:
        print_fn(f"Wrote {env_path} (provider: {provider}). Still needed before boot:")
        for gap in gaps:
            print_fn(f"  - {gap}")
        print_fn("")
        if provider == "docker":
            print_fn("Start host-run drukbox, then re-run the installer.")
        else:
            print_fn("Complete the remote shape values, then re-run the installer.")
    else:
        print_fn(f"✓ {env_path} is complete (provider: {provider}).")

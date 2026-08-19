import base64
import copy
import os
import re
import secrets
import tomllib
from collections.abc import Callable, Mapping, MutableMapping
from pathlib import Path
from typing import Any

import tomlkit

GAPS_EXIT_CODE = 3

_COMPOSE_ENV_KEYS = (
    "DRUKS_UID",
    "DRUKS_GID",
    "DRUKS_TAG",
    "DRUKS_WEB_BIND_HOST",
    "DRUKS_DOCKER_GID",
    "COMPOSE_FILE",
)
_ENV_KEY_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

# Env keys druks owns — setup renders them into .env, or the app reads their
# value from druks.toml directly. [env] and provider tables may not carry them.
_OWNED_ENV_KEYS = frozenset(
    {
        "DRUKS_POSTGRES_PASSWORD",
        "DRUKS_DATA_DIR",
        "DRUKS_UPSTREAM",
        "DRUKS_CLAUDE_HOME",
        "DRUKS_CODEX_HOME",
        "DRUKS_CLAUDE_JSON",
        "DRUKS_WEBHOOK_HOST",
        "DATABASE_URL",
        "REDIS_URL",
        "DRUKS_AUTH_HEADER",
        "DEFAULT_HOST_PROVIDER",
        "SERVICE_TOKENS",
        "DRUKS_AUTH_MODE",
        "DRUKS_AUTH_JWKS_URL",
        "DRUKS_AUTH_JWT_ISSUER",
        "DRUKS_AUTH_JWT_AUDIENCE",
        "DRUKS_AUTH_JWT_IDENTITY_CLAIM",
        "DRUKS_ENDPOINT",
        "DRUKS_SECRETS_KEY",
        "DRUKS_SANDBOX_SERVICE_URL",
        "DRUKS_SANDBOX_SERVICE_TOKEN",
        "DRUKS_SANDBOX_IMAGE",
    }
)
_KNOWN_TOML_KEYS = {
    "identity": (
        "mode",
        "header",
        "jwks_url",
        "jwt_issuer",
        "jwt_audience",
        "jwt_identity_claim",
    ),
    "urls": ("endpoint", "webhook_host"),
    "secrets": (
        "postgres_password",
        "secrets_key",
    ),
    "paths": ("data_dir", "claude_home", "codex_home", "claude_json"),
    "sandbox": (
        "provider",
        "service_url",
        "service_token",
        "image",
        "browser_login_proxy",
        "browser_login_tz",
        "timeout",
    ),
    "env": (),
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
    home: str,
    set_values: tuple[str, ...] = (),
    print_fn: Callable[[str], None] = print,
) -> int:
    toml_path = env_path.parent / "druks.toml"
    is_fresh = not toml_path.exists()
    is_changed = is_fresh
    if is_fresh:
        provider = provider.strip()
        if not provider:
            raise ValueError("sandbox provider cannot be blank")
        document = tomlkit.parse(_TOML_TEMPLATE)
        for value_path, value in _fresh_values(provider=provider, home=home):
            _set_value(document, value_path, value)
    else:
        document = tomlkit.parse(toml_path.read_text())

    for assignment in set_values:
        value_path, value = _parse_assignment(assignment)
        _set_value(document, value_path, value)
        is_changed = True

    provider = _get_string(document, ("sandbox", "provider"))
    print_fn(_shape_message(provider))

    if is_changed:
        _write_toml(toml_path, document)
    config = _read_toml(toml_path)
    if is_fresh:
        _write_gitignore(env_path.parent / ".gitignore")

    existing_env = env_path.read_text() if env_path.exists() else ""
    extras = {key: value for key, value in read_env(env_path).items() if key in _COMPOSE_ENV_KEYS}
    env_text = _render_env(config, extras=extras)
    _write_secure_text(env_path, env_text)

    if existing_env and existing_env != env_text:
        print_fn("Rendered .env changed. Apply it with: docker compose up -d")

    gaps = _collect_gaps(config)
    _print_outcome(print_fn, env_path=env_path, provider=provider, gaps=gaps)
    return GAPS_EXIT_CODE if gaps else 0


_TOML_TEMPLATE = """\
# druks.toml — the deployment. Edit this file, then re-run the installer
# to render and apply it. `druks setup` alone re-renders .env but does
# not restart services.

# Browser identity: "header", "jwt", or "none".
[identity]
mode = ""
header = ""
jwks_url = ""
jwt_issuer = ""
jwt_audience = ""
jwt_identity_claim = "email"

# Public dashboard and webhook ingress addresses.
[urls]
endpoint = ""
webhook_host = ""

# Generated on first write. Do not regenerate a deployed secret.
[secrets]
postgres_password = ""
secrets_key = ""

# Host paths.
[paths]
data_dir = ""
claude_home = ""
codex_home = ""
claude_json = ""

# Any drukbox provider name; docker and exe select install shapes.
[sandbox]
provider = ""
service_url = ""
service_token = ""
image = ""
# An HTTP proxy for the login window. The login then leaves from a different IP
# than the box. Use it for sign-in flows that refuse the box IP. Examples:
# http://172.17.0.1:8888, or http://user:pass@host:port for a proxy with a user
# name and password. If it is empty, the login uses the box IP. Only the login
# window uses it. See configuration.md.
browser_login_proxy = ""
# The timezone of the login browser. Use an IANA zone, for example
# "Europe/Madrid". Set it to the region of the login proxy. If it is empty, the
# browser keeps the container default.
browser_login_tz = ""
timeout = 180

# Put drukbox environment in [sandbox.<provider>]. The table is passed through
# to remote stacks verbatim; the local docker shape renders no provider table.
# Provider reference: https://github.com/czpython/drukbox (docs/deploy.md).

# Raw environment for processes druks does not model (drukbox, Caddy, libraries
# reading os.environ); keys render verbatim unless owned by druks.
[env]
"""


def _fresh_values(*, provider: str, home: str) -> tuple[tuple[tuple[str, ...], str], ...]:
    if provider == "docker":
        shape = (
            (("identity", "mode"), "none"),
            # The same origin every local doc and the installer banner print —
            # browser flows built from the endpoint (the GitHub manifest
            # callback's BroadcastChannel) are origin-scoped.
            (("urls", "endpoint"), "http://127.0.0.1:8001"),
            (("sandbox", "service_url"), "http://127.0.0.1:8000"),
            (("sandbox", "service_token"), "dev-token"),
            (("sandbox", "image"), "ghcr.io/czpython/druks-sandbox:latest"),
        )
    elif provider == "exe":
        shape = (
            (("identity", "mode"), "header"),
            (("identity", "header"), "X-ExeDev-Email"),
            (("sandbox", "service_url"), "http://127.0.0.1:8780"),
            (("sandbox", "service_token"), _hex_secret()),
            (("sandbox", "exe", "EXE_API_TOKEN"), ""),
            (("sandbox", "exe", "TAILSCALE_TAILNET"), ""),
            (("sandbox", "exe", "TAILSCALE_OAUTH_CLIENT_ID"), ""),
            (("sandbox", "exe", "TAILSCALE_OAUTH_CLIENT_SECRET"), ""),
            (("sandbox", "exe", "EXE_API_URL"), "https://exe.dev"),
            (("sandbox", "exe", "EXE_DEFAULT_IMAGE"), "ghcr.io/boldsoftware/exeuntu:latest"),
            (("sandbox", "exe", "TAILSCALE_ENABLED"), "true"),
            (("sandbox", "exe", "TAILSCALE_API_TIMEOUT"), "30"),
            (("sandbox", "exe", "TAILSCALE_AUTH_TAGS"), "tag:sandbox"),
        )
    else:
        shape = (
            (("identity", "mode"), "header"),
            (("sandbox", "service_url"), "http://127.0.0.1:8780"),
            (("sandbox", "service_token"), _hex_secret()),
        )

    return (
        (("sandbox", "provider"), provider),
        (("secrets", "postgres_password"), _hex_secret()),
        (("secrets", "secrets_key"), _secrets_key()),
        (("paths", "data_dir"), f"{home.rstrip('/')}/druks-data"),
        (("paths", "claude_home"), f"{home.rstrip('/')}/.claude"),
        (("paths", "codex_home"), f"{home.rstrip('/')}/.codex"),
        (("paths", "claude_json"), f"{home.rstrip('/')}/.claude.json"),
        *shape,
    )


def _read_toml(path: Path) -> dict[str, Any]:
    with path.open("rb") as config_file:
        config = tomllib.load(config_file)
    return _canonical_config(config)


def _canonical_config(raw: dict[str, Any]) -> dict[str, Any]:
    """Validated copy with every known table and key present. Operator
    additions are welcome as flat scalars, one table deep; anything more
    structured is refused with its key named."""
    config = copy.deepcopy(raw)
    for table_name, keys in _KNOWN_TOML_KEYS.items():
        table = config.setdefault(table_name, {})
        if not isinstance(table, dict):
            raise ValueError(f"druks.toml: [{table_name}] must be a table")
        for key in keys:
            value = table.setdefault(key, "")
            if table_name == "sandbox" and key == "timeout":
                if type(value) not in (str, int, float):
                    raise ValueError("druks.toml: sandbox.timeout must be a number or string")
            elif not isinstance(value, str):
                raise ValueError(f"druks.toml: {table_name}.{key} must be a string")

    provider_tables = _provider_tables(config["sandbox"])
    for provider_name, provider_table in provider_tables.items():
        if not isinstance(provider_table, dict):
            raise ValueError(f"druks.toml: [sandbox.{provider_name}] must be a table")
        for key, value in provider_table.items():
            if not isinstance(value, str):
                raise ValueError(f"druks.toml: sandbox.{provider_name}.{key} must be a string")
            if not _ENV_KEY_PATTERN.fullmatch(key):
                raise ValueError(
                    f"druks.toml: sandbox.{provider_name} key {key!r} is not an env name"
                )

    for key, value in config["env"].items():
        if not isinstance(value, str):
            raise ValueError(f"druks.toml: env.{key} must be a string")

    for table_name, table in config.items():
        if not isinstance(table, dict):
            _require_scalar("", table_name, table)
            continue
        for key, value in table.items():
            if table_name in _KNOWN_TOML_KEYS and key in _KNOWN_TOML_KEYS[table_name]:
                continue
            if table_name == "sandbox" and key in provider_tables:
                continue
            _require_scalar(f"{table_name}.", key, value)
    return config


def _provider_tables(sandbox: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in sandbox.items() if key not in _KNOWN_TOML_KEYS["sandbox"]}


def _require_scalar(prefix: str, key: str, value: Any) -> None:
    if not isinstance(value, (str, int, float, bool)):
        raise ValueError(
            f"druks.toml: {prefix}{key} must be a plain value (string, number, or boolean)"
        )


def _get_string(source: Mapping[str, Any], path: tuple[str, ...]) -> str:
    current: Any = source
    for part in path:
        if not isinstance(current, Mapping):
            return ""
        current = current.get(part, "")
    return current if isinstance(current, str) else ""


def _set_value(target: MutableMapping[str, Any], path: tuple[str, ...], value: str) -> None:
    current: MutableMapping[str, Any] = target
    for part in path[:-1]:
        child = current.setdefault(part, {})
        if not isinstance(child, MutableMapping):
            raise ValueError(f"druks.toml: {'.'.join(path)} crosses a non-table value")
        current = child
    current[path[-1]] = value


def _parse_assignment(assignment: str) -> tuple[tuple[str, ...], str]:
    path_text, separator, value = assignment.partition("=")
    path = tuple(path_text.split("."))
    if not separator or len(path) < 2 or any(not part for part in path):
        raise ValueError(f"invalid --set {assignment!r}; expected key.path=value")
    return path, value


def _write_toml(path: Path, document: tomlkit.TOMLDocument) -> None:
    text = tomlkit.dumps(document)
    _canonical_config(tomllib.loads(text))
    _write_secure_text(path, text)


def _render_env(
    config: dict[str, Any],
    *,
    extras: dict[str, str],
) -> str:
    provider = _get_string(config, ("sandbox", "provider"))

    service_tokens = ""
    if provider != "docker":
        service_tokens = _get_string(config, ("sandbox", "service_token"))

    sections = (
        (
            "GENERATED SECRETS",
            (("DRUKS_POSTGRES_PASSWORD", _get_string(config, ("secrets", "postgres_password"))),),
        ),
        (
            "DEPLOYMENT DEFAULTS",
            (
                ("DRUKS_DATA_DIR", _get_string(config, ("paths", "data_dir"))),
                ("DRUKS_UPSTREAM", "127.0.0.1:8001"),
                ("DRUKS_CLAUDE_HOME", _get_string(config, ("paths", "claude_home"))),
                ("DRUKS_CODEX_HOME", _get_string(config, ("paths", "codex_home"))),
                ("DRUKS_CLAUDE_JSON", _get_string(config, ("paths", "claude_json"))),
                ("DRUKS_WEBHOOK_HOST", _get_string(config, ("urls", "webhook_host"))),
                ("DATABASE_URL", "sqlite+aiosqlite:////data/drukbox.db"),
                ("REDIS_URL", "redis://127.0.0.1:6379/2"),
            ),
        ),
        ("IDENTITY", (("DRUKS_AUTH_HEADER", _get_string(config, ("identity", "header"))),)),
        (
            "SANDBOX",
            (
                ("DEFAULT_HOST_PROVIDER", provider),
                ("SERVICE_TOKENS", service_tokens),
            ),
        ),
    )

    lines: list[str] = []
    for title, pairs in sections:
        section_lines = [_env_line(key, value) for key, value in pairs if value]
        if section_lines:
            lines.extend(("# " + "=" * 60, f"# {title}", "# " + "=" * 60, ""))
            lines.extend(section_lines)
            lines.append("")

    provider_environment: dict[str, str] = config["sandbox"].get(provider, {})
    if provider != "docker":
        pass_through = []
        for key, value in provider_environment.items():
            if _is_reserved_env_key(key) or not value:
                continue
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

    deployment_env: dict[str, str] = config["env"]
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


def _is_reserved_env_key(key: str) -> bool:
    return key.startswith("DRUKS_") or key in _OWNED_ENV_KEYS


def _collect_gaps(config: dict[str, Any]) -> list[str]:
    gaps = [
        f"{'.'.join(path)} is empty"
        for path in (
            ("secrets", "postgres_password"),
            ("secrets", "secrets_key"),
            ("identity", "mode"),
            ("sandbox", "provider"),
            ("sandbox", "service_url"),
            ("sandbox", "service_token"),
        )
        if not _get_string(config, path)
    ]

    identity_mode = _get_string(config, ("identity", "mode"))
    if identity_mode not in {"none", "header", "jwt"}:
        gaps.append("identity.mode must be one of: header, jwt, none")
    if identity_mode in {"header", "jwt"} and not _get_string(config, ("identity", "header")):
        gaps.append("identity.header is empty")
    if identity_mode == "jwt":
        for path in (
            ("identity", "jwks_url"),
            ("identity", "jwt_issuer"),
            ("identity", "jwt_audience"),
            ("identity", "jwt_identity_claim"),
        ):
            if not _get_string(config, path):
                gaps.append(f"{'.'.join(path)} is empty")

    provider = _get_string(config, ("sandbox", "provider"))
    provider_tables = _provider_tables(config["sandbox"])
    for provider_name, provider_environment in provider_tables.items():
        if provider_name != provider:
            gaps.append(f"sandbox.{provider_name} is a stale provider table")
        for key in provider_environment:
            if _is_reserved_env_key(key):
                gaps.append(f"sandbox.{provider_name}.{key} is reserved by druks")

    deployment_env: dict[str, str] = config["env"]
    for key in deployment_env:
        if key in _OWNED_ENV_KEYS:
            gaps.append(f"env.{key} is reserved by druks")

    provider_environment = provider_tables.get(provider, {})
    if provider == "exe":
        for key in ("EXE_API_TOKEN", "TAILSCALE_TAILNET"):
            if not _get_string(config, ("sandbox", "exe", key)):
                gaps.append(f"sandbox.exe.{key} is empty")
    elif provider != "docker" and not any(
        value for key, value in provider_environment.items() if not _is_reserved_env_key(key)
    ):
        gaps.append(
            f"[sandbox.{provider}] has no configured values for remote provider {provider!r}"
        )
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
    missing = [entry for entry in ("druks.toml", ".env") if entry not in existing]
    if missing:
        body = "\n".join((*existing, *missing)).lstrip("\n") + "\n"
        path.write_text(body)


def _shape_message(provider: str) -> str:
    if not provider:
        return "sandbox provider is not configured"
    if provider == "docker":
        return "provider 'docker' → local shape; drukbox validates its configuration"
    if provider == "exe":
        return "provider 'exe' → exe shape; check `druks doctor` after boot"
    return (
        f"provider {provider!r} → remote shape; drukbox validates the name — "
        "check `druks doctor` after boot"
    )


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
        print_fn("Set the values in druks.toml, then re-run the installer.")
    else:
        print_fn(f"✓ {env_path} is complete (provider: {provider}).")

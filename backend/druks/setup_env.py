import base64
import copy
import os
import re
import secrets
import tomllib
from collections.abc import Callable, Mapping, MutableMapping
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import tomlkit

from druks.core.utils.time import validate_timezone
from druks.settings import Settings, secret_variable

GAPS_EXIT_CODE = 3

_COMPOSE_ENV_KEYS = (
    "DRUKS_UID",
    "DRUKS_GID",
    "DRUKS_TAG",
    "DRUKS_WEB_BIND_HOST",
    "DRUKS_DOCKER_GID",
    "COMPOSE_FILE",
    "COMPOSE_PROFILES",
    "DRUKS_SBX_HOME",
)
_ENV_KEY_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

# The variables of each secret that setup knows, by its druks.toml key. Setup moves
# such a secret from druks.toml to the secrets section of .env. The secrets.* keys
# and the env.* keys cover a druks.toml from before that section.
_SECRET_VARIABLES = {
    **{key: (secret_variable(key),) for key in Settings.secret_keys()},
    "sandbox.service_token": (secret_variable("sandbox.service_token"), "SERVICE_TOKENS"),
    "sandbox.registry_password": ("REGISTRY_PASSWORD",),
    "sandbox.exe.EXE_API_TOKEN": ("EXE_API_TOKEN",),
    "sandbox.exe.TAILSCALE_OAUTH_CLIENT_SECRET": ("TAILSCALE_OAUTH_CLIENT_SECRET",),
    "secrets.secrets_key": ("DRUKS_SECRETS_KEY",),
    "secrets.postgres_password": ("DRUKS_POSTGRES_PASSWORD",),
    "secrets.drukbox_secrets_key": ("SECRETS_KEY",),
    "env.DRUKS_DATABASE_URL": ("DRUKS_DATABASE_URL",),
    "env.DRUKS_REDIS_URL": ("DRUKS_REDIS_URL",),
}
_KNOWN_SECRETS = frozenset(name for names in _SECRET_VARIABLES.values() for name in names)
_SECRETS_TITLE = "SECRETS"

# Env keys druks owns — setup renders them into .env, the app reads their value
# from druks.toml directly, or they are known secrets. [env] and provider tables
# may not carry them.
_OWNED_ENV_KEYS = frozenset(
    {
        "DRUKS_POSTGRES_PASSWORD",
        "DRUKS_DATA_DIR",
        "DRUKS_UPSTREAM",
        "DRUKS_HARNESS_CONFIG_ROOT",
        "DRUKS_WEBHOOK_HOST",
        "DRUKS_ISSUER_HOST",
        "DRUKS_ISSUER_BIND_HOST",
        "DATABASE_URL",
        "REDIS_URL",
        "DRUKS_AUTH_HEADER",
        "DEFAULT_HOST_PROVIDER",
        "REGISTRY_HOST",
        "REGISTRY_USERNAME",
        "REGISTRY_PASSWORD",
        "TEMPLATE_REPOSITORY",
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
        "SECRETS_KEY",
        "SECRETS_PROXY_URL",
        "SECRETS_PROXY_CA_FILE",
        "SECRETS_EXCHANGE_URL",
        "SECRETS_EXCHANGE_BIND_HOST",
        "SECRETS_EXCHANGE_PORT",
        "DRUKS_SECRETS_PROXY_BIND_HOST",
    }
    | _KNOWN_SECRETS
)
_KNOWN_TOP_LEVEL_KEYS = frozenset({"timezone"})
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
    "paths": ("data_dir", "harness_config_root"),
    "sandbox": (
        "provider",
        "service_url",
        "image",
        "registry_host",
        "registry_username",
        "template_repository",
        "proxy_url",
        "issuer_url",
        "timeout",
    ),
    "browser": ("proxy_scope", "timezone"),
    "env": (),
}


def _hex_secret() -> str:
    return secrets.token_hex(32)


def _secrets_key() -> str:
    return base64.b64encode(secrets.token_bytes(32)).decode()


def read_env(path: Path) -> dict[str, str]:
    return _parse_env(path.read_text()) if path.exists() else {}


def _parse_env(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in text.splitlines():
        key, separator, value = line.partition("=")
        if separator and not line.startswith("#"):
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

    existing_env = env_path.read_text() if env_path.exists() else ""
    secrets = _read_secrets(existing_env)
    is_changed = _move_secrets(document, secrets) or is_changed
    _generate_secrets(secrets)

    provider = _get_string(document, ("sandbox", "provider"))
    print_fn(_shape_message(provider))

    toml_text = tomlkit.dumps(document)
    config = _canonical_config(tomllib.loads(toml_text))
    extras = {key: value for key, value in read_env(env_path).items() if key in _COMPOSE_ENV_KEYS}
    env_text = _render_env(config, extras=extras, secrets=secrets)
    # .env holds the secrets. Replace it in one step, and before druks.toml loses
    # a secret that this run moves.
    partial_env_path = env_path.with_name(f"{env_path.name}.tmp")
    _write_secure_text(partial_env_path, env_text)
    os.replace(partial_env_path, env_path)
    if is_changed:
        _write_secure_text(toml_path, toml_text)
    if is_fresh:
        _write_gitignore(env_path.parent / ".gitignore")

    if existing_env and existing_env != env_text:
        print_fn("Rendered .env changed. Apply it with: docker compose up -d")

    gaps = _collect_gaps(config, secrets)
    _print_outcome(print_fn, env_path=env_path, provider=provider, gaps=gaps)
    return GAPS_EXIT_CODE if gaps else 0


_TOML_TEMPLATE = """\
# druks.toml — the deployment. Edit this file, then re-run the installer
# to render and apply it. `druks setup` alone re-renders .env but does
# not restart services. This file holds no secret: the secrets are in the
# last section of .env. See configuration.md.

# Schedule timezone and initial timezone for new accounts.
timezone = "UTC"

# Browser identity: "header", "jwt", or "none".
[identity]
mode = ""
header = ""
jwks_url = ""
jwt_issuer = ""
jwt_audience = ""
jwt_identity_claim = "/email"

# Public dashboard and webhook ingress addresses.
[urls]
endpoint = ""
webhook_host = ""

# Host paths.
[paths]
data_dir = ""
harness_config_root = ""

# Any drukbox provider name; docker and exe select install shapes.
[sandbox]
provider = ""
service_url = ""
image = ""
# Access to private sandbox images on one registry host, for example ghcr.io.
# The password is the secret REGISTRY_PASSWORD in .env.
registry_host = ""
registry_username = ""
# The repository path on that host where drukbox publishes sandbox templates.
# The exe provider requires it.
template_repository = ""
# The secrets proxy, at the address a sandbox dials. A sandbox sends its HTTPS
# through it. The docker shape uses the Docker bridge gateway. A remote shape
# names the address of this host that its sandboxes reach, for example the
# tailnet address on exe. docker-sbx runs no proxy and leaves it empty.
proxy_url = ""
# The issuer base URL the secrets exchange dials; loopback web by default. For a
# drukbox on another server, set the address of this host that drukbox reaches.
issuer_url = ""
timeout = 180

# Put drukbox environment in [sandbox.<provider>]. The table is passed through
# to remote stacks verbatim; the local docker shape renders no provider table.
# Put a provider secret in the secrets section of .env, not in this table.
# Provider reference: https://github.com/czpython/drukbox (docs/deploy.md).

# The browser that apps borrow and the operator signs into.
[browser]
# Which launches leave through the browser proxy, the secret DRUKS_BROWSER_PROXY
# in .env. "login" (the default) sends only the login window through it. "all"
# sends the login window and every borrow.
proxy_scope = ""
# The timezone of the proxied browser. Use an IANA zone, for example
# "Europe/Madrid", in the region of the proxy. If it is empty, the browser
# keeps the container default.
timezone = ""

# Raw environment for processes druks does not model (drukbox, Caddy, libraries
# reading os.environ); keys render verbatim unless owned by druks.
[env]
"""


def _fresh_values(*, provider: str, home: str) -> tuple[tuple[tuple[str, ...], str], ...]:
    if provider == "docker":
        shape = (
            (("identity", "mode"), "none"),
            # Browser flows built from the endpoint are origin-scoped, so this
            # matches the origin every local doc prints.
            (("urls", "endpoint"), "http://127.0.0.1:8001"),
            (("sandbox", "service_url"), "http://127.0.0.1:8780"),
            (("sandbox", "image"), "ghcr.io/czpython/druks/sandbox:latest"),
            # Sandbox containers reach the host at the bridge gateway.
            (("sandbox", "proxy_url"), "http://172.17.0.1:8880"),
        )
    elif provider == "exe":
        shape = (
            (("identity", "mode"), "header"),
            (("identity", "header"), "X-ExeDev-Email"),
            (("sandbox", "service_url"), "http://127.0.0.1:8780"),
            (("sandbox", "exe", "TAILSCALE_TAILNET"), ""),
            (("sandbox", "exe", "TAILSCALE_OAUTH_CLIENT_ID"), ""),
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
        )

    return (
        (("sandbox", "provider"), provider),
        (("paths", "data_dir"), f"{home.rstrip('/')}/druks-data"),
        (("paths", "harness_config_root"), f"{home.rstrip('/')}/.config/druks/harnesses"),
        *shape,
    )


def _canonical_config(raw: dict[str, Any]) -> dict[str, Any]:
    """Validated copy with every known table and key present. Operator additions
    are flat scalars, one table deep; more structure is refused by key."""
    config = copy.deepcopy(raw)
    timezone = config.setdefault("timezone", "UTC")
    if not isinstance(timezone, str):
        raise ValueError("druks.toml: timezone must be a string")
    validate_timezone(timezone)
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


def _read_secrets(env_text: str) -> dict[str, str]:
    """The secrets that .env holds: each variable of its secrets section, and each
    known secret above that section."""
    head, _, section = env_text.partition(f"\n# {_SECRETS_TITLE}\n")
    secrets = {key: value for key, value in _parse_env(head).items() if key in _KNOWN_SECRETS}
    for key, value in _parse_env(section).items():
        # install.sh appends its compose keys to the end of the file.
        if key not in _COMPOSE_ENV_KEYS:
            secrets[key] = value
    return {key: value for key, value in secrets.items() if value}


def _move_secrets(document: tomlkit.TOMLDocument, secrets: dict[str, str]) -> bool:
    """Move each known secret that druks.toml holds to ``secrets``."""
    is_moved = False
    for key, variables in _SECRET_VARIABLES.items():
        *table_path, name = key.split(".")
        table: Any = document
        for part in table_path:
            table = table.get(part, {}) if isinstance(table, MutableMapping) else {}
        if isinstance(table, MutableMapping) and name in table:
            if value := _get_string(table, (name,)):
                secrets.update(dict.fromkeys(variables, value))
            del table[name]
            is_moved = True
    if "secrets" in document and not document["secrets"]:
        del document["secrets"]
    return is_moved


def _generate_secrets(secrets: dict[str, str]) -> None:
    """Add each secret that setup can make and that ``secrets`` does not hold."""
    secrets.setdefault("DRUKS_SECRETS_KEY", _secrets_key())
    secrets.setdefault("SECRETS_KEY", _secrets_key())
    secrets.setdefault("DRUKS_POSTGRES_PASSWORD", _hex_secret())
    token = secrets.setdefault(secret_variable("sandbox.service_token"), _hex_secret())
    # drukbox refuses to start without SERVICE_TOKENS. A compose-side default
    # would replace that safe stop with a known token.
    secrets.setdefault("SERVICE_TOKENS", token)


def _parse_assignment(assignment: str) -> tuple[tuple[str, ...], str]:
    path_text, separator, value = assignment.partition("=")
    path = tuple(path_text.split("."))
    if (
        not separator
        or (len(path) < 2 and path[0] not in _KNOWN_TOP_LEVEL_KEYS)
        or any(not part for part in path)
    ):
        raise ValueError(f"invalid --set {assignment!r}; expected key.path=value")
    return path, value


def _render_env(
    config: dict[str, Any],
    *,
    extras: dict[str, str],
    secrets: dict[str, str],
) -> str:
    provider = _get_string(config, ("sandbox", "provider"))
    proxy_url = _get_string(config, ("sandbox", "proxy_url"))
    issuer_url = _get_string(config, ("sandbox", "issuer_url"))

    sections = (
        (
            "DEPLOYMENT DEFAULTS",
            (
                ("DRUKS_DATA_DIR", _get_string(config, ("paths", "data_dir"))),
                (
                    "DRUKS_HARNESS_CONFIG_ROOT",
                    _get_string(config, ("paths", "harness_config_root")),
                ),
                ("DRUKS_UPSTREAM", "127.0.0.1:8001"),
                ("DRUKS_WEBHOOK_HOST", _get_string(config, ("urls", "webhook_host"))),
                ("DRUKS_ISSUER_HOST", issuer_url),
                # The issuer listener binds the address drukbox dials and nothing else.
                ("DRUKS_ISSUER_BIND_HOST", urlsplit(issuer_url).hostname or ""),
                ("DATABASE_URL", "sqlite+aiosqlite:////data/drukbox.db"),
                ("REDIS_URL", "redis://127.0.0.1:6379/2"),
            ),
        ),
        ("IDENTITY", (("DRUKS_AUTH_HEADER", _get_string(config, ("identity", "header"))),)),
        (
            "SANDBOX",
            (
                ("DEFAULT_HOST_PROVIDER", provider),
                ("REGISTRY_HOST", _get_string(config, ("sandbox", "registry_host"))),
                ("REGISTRY_USERNAME", _get_string(config, ("sandbox", "registry_username"))),
                ("TEMPLATE_REPOSITORY", _get_string(config, ("sandbox", "template_repository"))),
                ("SECRETS_PROXY_URL", proxy_url),
                # The proxy binds the address sandboxes dial and nothing else.
                ("DRUKS_SECRETS_PROXY_BIND_HOST", urlsplit(proxy_url).hostname or ""),
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

    # The last section, because setup reads it back to the end of the file.
    lines.extend(
        (
            "# " + "=" * 60,
            f"# {_SECRETS_TITLE}",
            "# " + "=" * 60,
            "# druks setup makes these values once and keeps each line of this section.",
            "# Add your own secrets here, for example a provider token.",
            "",
            *(_env_line(key, value) for key, value in secrets.items()),
        )
    )
    return "\n".join(lines).rstrip() + "\n"


def _env_line(key: str, value: str) -> str:
    if "\n" in value or "\r" in value:
        raise ValueError(f".env value for {key} may not contain a newline")
    return f"{key}={value}"


def _is_reserved_env_key(key: str) -> bool:
    return key.startswith("DRUKS_") or key in _OWNED_ENV_KEYS


def _collect_gaps(config: dict[str, Any], secrets: dict[str, str]) -> list[str]:
    gaps = [
        f"{'.'.join(path)} is empty"
        for path in (
            ("identity", "mode"),
            ("sandbox", "provider"),
            ("sandbox", "service_url"),
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
    for key in sorted(secrets.keys() & (deployment_env.keys() | provider_environment.keys())):
        gaps.append(
            f"{key} is in druks.toml and in the secrets section of .env. Remove one of them."
        )

    if provider == "exe":
        if "EXE_API_TOKEN" not in secrets:
            gaps.append("EXE_API_TOKEN is empty. Add it to the secrets section of .env.")
        if not _get_string(config, ("sandbox", "exe", "TAILSCALE_TAILNET")):
            gaps.append("sandbox.exe.TAILSCALE_TAILNET is empty")
        if not _get_string(config, ("sandbox", "proxy_url")):
            gaps.append("sandbox.proxy_url is empty")
    elif provider != "docker" and not any(
        value
        for key, value in (provider_environment | secrets).items()
        if not _is_reserved_env_key(key)
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
        print_fn("Set the values in druks.toml and .env, then re-run the installer.")
    else:
        print_fn(f"✓ {env_path} is complete (provider: {provider}).")

import base64
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

GAPS_EXIT_CODE = 3


@dataclass(frozen=True)
class Entry:
    key: str
    # Static default, or a zero-arg callable evaluated once on first write
    # (secrets). ``{install_dir}`` / ``{home}`` placeholders are expanded.
    default: str | Callable[[], str] = ""
    prompt: str | None = None
    required: bool = False
    comment: str | None = None


@dataclass(frozen=True)
class Section:
    title: str
    entries: tuple[Entry, ...] = ()
    # Free-form commented lines appended after the entries (documented
    # optional knobs the operator uncomments by hand).
    trailer: str | None = None


def _hex_secret() -> str:
    return secrets.token_hex(32)


def _announce_secret() -> str:
    return secrets.token_urlsafe(40).replace("-", "").replace("_", "")[:43]


def _secrets_key() -> str:
    return base64.b64encode(secrets.token_bytes(32)).decode()


_COMMON_SECTIONS: tuple[Section, ...] = (
    Section(
        "EDIT THESE (the installer refuses to boot until they're filled in)",
        (
            # Where druks may act is NOT configured here — it's wherever the
            # operator GitHub App is installed. Install/uninstall the App to
            # grant/revoke; `druks doctor` shows the effective set.
            Entry(
                "GITHUB_OPERATOR_APP_ID",
                prompt="Operator GitHub App id (or run install.sh --apps later)",
                required=True,
                comment=(
                    "GitHub Apps — provision via ``install.sh --apps`` (manifest\n"
                    "flow, fills these in) or create by hand and upload the PEMs."
                ),
            ),
            Entry("GITHUB_OPERATOR_PRIVATE_KEY_PATH", "{install_dir}/secrets/operator.pem"),
            Entry("GITHUB_OPERATOR_PEM", "{install_dir}/secrets/operator.pem"),
            Entry(
                "GITHUB_REVIEWER_APP_ID",
                prompt="Reviewer GitHub App id (or run install.sh --apps later)",
                required=True,
            ),
            Entry("GITHUB_REVIEWER_PRIVATE_KEY_PATH", "{install_dir}/secrets/reviewer.pem"),
            Entry("GITHUB_REVIEWER_PEM", "{install_dir}/secrets/reviewer.pem"),
            Entry(
                "LINEAR_API_KEY",
                prompt="Linear API key (enter to skip Linear)",
                comment="Linear (optional — leave blank to skip)",
            ),
            Entry("LINEAR_WEBHOOK_SECRET", prompt="Linear webhook secret (enter to skip)"),
            Entry(
                "DRUKS_ENDPOINT",
                prompt="Base URL the operator's browser reaches druks at (enter to skip)",
                comment=(
                    "Public base URL of the dashboard, e.g. https://druks.example.com —\n"
                    "needed only to connect OAuth MCP servers (it hosts their callback)."
                ),
            ),
        ),
    ),
    Section(
        "AUTO-GENERATED — do not regenerate without redeploying the stack",
        (
            # Postgres reads it at initdb only — regenerating later means
            # ALTER ROLE (or a fresh volume), not just a redeploy.
            Entry("DRUKS_POSTGRES_PASSWORD", _hex_secret),
            Entry("DRUKS_WEBHOOK_SECRET", _hex_secret),
            Entry("ANNOUNCE_TOKEN_SECRET", _announce_secret),
            # Encrypts stored secrets (MCP tokens, OAuth grants) at rest.
            # Rotatable: comma-separated, first key encrypts, all decrypt —
            # see docs/configuration.md.
            Entry("DRUKS_SECRETS_KEY", _secrets_key),
        ),
    ),
    Section(
        "DEFAULTS (rarely changed)",
        (
            Entry("DRUKS_DATA_DIR", "{home}/druks-data"),
            Entry("DRUKS_UPSTREAM", "127.0.0.1:8001"),
            Entry("DRUKS_CLAUDE_HOME", "{home}/.claude"),
            Entry("DRUKS_CODEX_HOME", "{home}/.codex"),
            Entry("DRUKS_CLAUDE_JSON", "{home}/.claude.json"),
            Entry(
                "DRUKS_WEBHOOK_HOST",
                comment=(
                    "Public webhook hostname (e.g. druks.example.com). Point an\n"
                    "A-record at this box and open 80/443; Caddy auto-provisions\n"
                    "TLS and serves only /_external/*. Leave empty when the edge\n"
                    "already carries webhooks (exe.dev port-share) or you bring\n"
                    "your own ingress."
                ),
            ),
            Entry(
                "DATABASE_URL",
                "sqlite+aiosqlite:////data/drukbox.db",
                comment=(
                    "Drukbox internals. DATABASE_URL is overridden by compose to the\n"
                    "shared SQLite file; kept here only as a documented default for\n"
                    "non-compose runs."
                ),
            ),
            Entry("REDIS_URL", "redis://127.0.0.1:6379/2"),
        ),
    ),
)

# How each install shape resolves browser identity. Both Caddy and druks read
# DRUKS_AUTH_HEADER: Caddy requires the edge's assertion, druks maps it to an
# account.
_EXE_IDENTITY_SECTION = Section(
    "IDENTITY — exe.dev authenticates at the edge; druks maps the asserted email",
    (
        Entry("DRUKS_AUTH_MODE", "header"),
        Entry("DRUKS_AUTH_HEADER", "X-ExeDev-Email"),
    ),
)

_AWS_IDENTITY_SECTION = Section(
    "IDENTITY — your edge proxy authenticates; druks maps the asserted email",
    (
        Entry("DRUKS_AUTH_MODE", "header"),
        Entry(
            "DRUKS_AUTH_HEADER",
            prompt="Identity header your edge proxy injects (e.g. X-Forwarded-Email)",
            required=True,
            comment=(
                "The header your identity proxy (Teleport, Cloudflare Access, …)\n"
                "injects after authenticating. The proxy must strip any\n"
                "client-supplied copy before inserting its own."
            ),
        ),
    ),
)

_LOCAL_IDENTITY_SECTION = Section(
    "IDENTITY — loopback dashboard, no authentication; one operator account",
    (Entry("DRUKS_AUTH_MODE", "none"),),
)

_IDENTITY_SECTIONS = {
    "exe": _EXE_IDENTITY_SECTION,
    "aws": _AWS_IDENTITY_SECTION,
    "docker": _LOCAL_IDENTITY_SECTION,
}

# How druks reaches drukbox. The remote shapes run drukbox itself from this
# same .env (compose `remote` profile): container on :8780, generated token;
# SERVICE_TOKENS is drukbox's accepted-token list, mirrored from the sandbox
# token when blank.
_REMOTE_DRUKBOX_ENTRIES: tuple[Entry, ...] = (
    Entry("DRUKS_SANDBOX_SERVICE_URL", "http://127.0.0.1:8780"),
    Entry("DRUKS_SANDBOX_SERVICE_TOKEN", _hex_secret),
    Entry("SERVICE_TOKENS"),
    Entry(
        "DRUKS_SANDBOX_IMAGE",
        comment="Leave empty so drukbox picks per-provider; set as override only.",
    ),
)

_AWS_SECTION = Section(
    "SANDBOX PROVIDER — AWS EC2 (drukbox)",
    (
        Entry(
            "AWS_REGION",
            prompt="AWS region (e.g. eu-central-1)",
            required=True,
            comment=(
                "Control-plane credentials reach boto via the container env; an\n"
                "IAM instance profile on this box works too — leave the key pair\n"
                "blank and boto's default chain picks the role up automatically."
            ),
        ),
        Entry("AWS_DEFAULT_IMAGE", prompt="AMI id for sandbox VMs", required=True),
        Entry("AWS_ACCESS_KEY_ID", prompt="AWS access key id (enter to use instance profile)"),
        Entry("AWS_SECRET_ACCESS_KEY", prompt="AWS secret access key (enter to skip)"),
        Entry("DEFAULT_HOST_PROVIDER", "aws"),
        Entry("TAILSCALE_ENABLED", "false"),
        Entry("AWS_INSTANCE_TYPE", "t3.medium"),
        *_REMOTE_DRUKBOX_ENTRIES,
    ),
    trailer=(
        "# AWS_SUBNET_ID=          # default VPC subnet if unset\n"
        "# AWS_SECURITY_GROUP_ID=  # drukbox manages one if unset\n"
        "# AWS_SSH_USERNAME=ubuntu # override if the AMI uses a different user\n"
        "# AWS_INSTANCE_PROFILE=   # IAM profile attached to the SANDBOX VMs\n"
        "#                         # drukbox launches (not control-plane auth)"
    ),
)

_EXE_SECTION = Section(
    "SANDBOX PROVIDER — exe.dev + Tailscale (drukbox)",
    (
        Entry(
            "TAILSCALE_TAILNET",
            prompt='Tailscale magic-DNS suffix (e.g. "yourtail.ts.net")',
            required=True,
        ),
        Entry("TAILSCALE_OAUTH_CLIENT_ID", prompt="Tailscale OAuth client id"),
        Entry("TAILSCALE_OAUTH_CLIENT_SECRET", prompt="Tailscale OAuth client secret"),
        Entry("EXE_API_TOKEN", prompt="exe.dev API token", required=True),
        Entry("DEFAULT_HOST_PROVIDER", "exe"),
        Entry("TAILSCALE_ENABLED", "true"),
        Entry("EXE_API_URL", "https://exe.dev"),
        Entry("EXE_DEFAULT_IMAGE", "ghcr.io/boldsoftware/exeuntu:latest"),
        Entry("TAILSCALE_API_TIMEOUT", "30"),
        Entry("TAILSCALE_AUTH_TAGS", "tag:sandbox"),
        *_REMOTE_DRUKBOX_ENTRIES,
    ),
)

_LOCAL_SECTION = Section(
    "SANDBOX PROVIDER — local Docker containers (drukbox on the host)",
    (
        Entry("DEFAULT_HOST_PROVIDER", "docker"),
        # drukbox runs on the host (`make dev`: port 8000, fixed dev-token)
        # and reads its own env there — nothing in this file configures it.
        Entry("DRUKS_SANDBOX_SERVICE_URL", "http://127.0.0.1:8000"),
        Entry("DRUKS_SANDBOX_SERVICE_TOKEN", "dev-token"),
        Entry("DRUKS_SANDBOX_IMAGE", "ghcr.io/czpython/druks-sandbox:latest"),
    ),
    trailer=(
        "# Sandboxes are local Docker containers. Run drukbox on the host so\n"
        "# its docker provider reaches your Docker daemon:\n"
        "#   git clone https://github.com/czpython/drukbox\n"
        "#   cd drukbox && DOCKER_SSH_USERNAME=druks make dev"
    ),
)

_PROVIDER_SECTIONS = {"exe": _EXE_SECTION, "aws": _AWS_SECTION, "docker": _LOCAL_SECTION}


def sections_for(provider: str) -> tuple[Section, ...]:
    return (*_COMMON_SECTIONS, _IDENTITY_SECTIONS[provider], _PROVIDER_SECTIONS[provider])


def read_env(path: Path) -> dict[str, str]:
    """KEY=VALUE lines → dict. Comments/blank lines skipped; values kept raw
    (quotes and all) so we round-trip exactly what the operator wrote."""
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        values[key.strip()] = value.strip()
    return values


def _is_blank(value: str | None) -> bool:
    return value is None or value.strip().strip("\"'") == ""


def run_setup(
    env_path: Path,
    *,
    provider: str,
    install_dir: str,
    home: str,
    interactive: bool,
    input_fn: Callable[[str], str] = input,
    print_fn: Callable[[str], None] = print,
) -> int:
    fresh = not env_path.exists()
    existing = read_env(env_path)
    # A re-run keeps the provider the .env was written with; the flag only
    # decides the very first write.
    provider = existing.get("DEFAULT_HOST_PROVIDER", "").strip() or provider
    if provider not in _PROVIDER_SECTIONS:
        print_fn(f"unknown provider {provider!r} (expected one of {', '.join(_PROVIDER_SECTIONS)})")
        return 1

    sections = sections_for(provider)
    placeholders = {"install_dir": install_dir.rstrip("/"), "home": home.rstrip("/")}

    values: dict[str, str] = {}
    for section in sections:
        for entry in section.entries:
            current = existing.get(entry.key)
            if not _is_blank(current):
                values[entry.key] = current  # type: ignore[assignment]
                continue
            default = entry.default() if callable(entry.default) else entry.default
            values[entry.key] = default.format(**placeholders)
    # drukbox's accepted-token list mirrors the sandbox token (remote shapes
    # only — the local shape's drukbox brings its own env).
    if "SERVICE_TOKENS" in values and _is_blank(values["SERVICE_TOKENS"]):
        values["SERVICE_TOKENS"] = values["DRUKS_SANDBOX_SERVICE_TOKEN"]
    # A local install's dashboard sits at a fixed localhost port, so its
    # OAuth-MCP callback base is knowable up front — default it so connecting
    # an OAuth MCP server (e.g. Linear) works out of the box. Remote shapes
    # reach the dashboard through their own edge, so they supply this.
    if provider == "docker" and _is_blank(values.get("DRUKS_ENDPOINT")):
        values["DRUKS_ENDPOINT"] = "http://localhost:8001"

    if interactive:
        for section in sections:
            for entry in section.entries:
                if not entry.prompt or not _is_blank(values.get(entry.key)):
                    continue
                # Optional values get one offer — on the fresh write. Re-runs
                # only nag about what actually blocks the boot; a blank
                # optional is a decision, not a gap.
                if not entry.required and not fresh:
                    continue
                answer = input_fn(f"{entry.prompt}: ").strip()
                if answer:
                    values[entry.key] = answer

    template_keys = {entry.key for section in sections for entry in section.entries}
    extras = {key: value for key, value in existing.items() if key not in template_keys}

    _write_env(env_path, sections, values, extras)
    (env_path.parent / "secrets").mkdir(mode=0o700, exist_ok=True)

    gaps = _collect_gaps(sections, values, env_path=env_path, install_dir=install_dir)
    aws_keys_blank = provider == "aws" and _is_blank(values.get("AWS_ACCESS_KEY_ID"))
    _print_outcome(
        print_fn,
        env_path=env_path,
        provider=provider,
        gaps=gaps,
        aws_keys_blank=aws_keys_blank,
    )
    return 0 if not gaps else GAPS_EXIT_CODE


def _write_env(
    env_path: Path,
    sections: tuple[Section, ...],
    values: dict[str, str],
    extras: dict[str, str],
) -> None:
    lines: list[str] = []
    for section in sections:
        lines.append("# " + "=" * 60)
        lines.append(f"# {section.title}")
        lines.append("# " + "=" * 60)
        lines.append("")
        for entry in section.entries:
            if entry.comment:
                lines.extend(f"# {comment_line}" for comment_line in entry.comment.splitlines())
            lines.append(f"{entry.key}={values.get(entry.key, '')}")
        if section.trailer:
            lines.append(section.trailer)
        lines.append("")
    if extras:
        lines.append("# " + "=" * 60)
        lines.append("# OPERATOR ADDITIONS (preserved verbatim by druks setup)")
        lines.append("# " + "=" * 60)
        lines.append("")
        lines.extend(f"{key}={value}" for key, value in extras.items())
        lines.append("")
    env_path.write_text("\n".join(lines))
    env_path.chmod(0o600)


def _collect_gaps(
    sections: tuple[Section, ...],
    values: dict[str, str],
    *,
    env_path: Path,
    install_dir: str,
) -> list[str]:
    gaps = [
        f"{entry.key} is empty"
        for section in sections
        for entry in section.entries
        if entry.required and _is_blank(values.get(entry.key))
    ]
    # PEM files must exist to boot. The configured paths are HOST paths;
    # when they live under the install dir we can check them through the
    # bootstrap mount (env_path's directory IS the install dir).
    prefix = install_dir.rstrip("/") + "/"
    for key in ("GITHUB_OPERATOR_PEM", "GITHUB_REVIEWER_PEM"):
        configured = values.get(key, "").strip()
        if not configured.startswith(prefix):
            continue  # custom location — doctor verifies it post-boot
        local = env_path.parent / configured.removeprefix(prefix)
        if not local.is_file():
            gaps.append(f"{configured} is missing (upload the PEM, or run install.sh --apps)")
    return gaps


def _print_outcome(
    print_fn: Callable[[str], None],
    *,
    env_path: Path,
    provider: str,
    gaps: list[str],
    aws_keys_blank: bool,
) -> None:
    if not gaps:
        print_fn(f"✓ {env_path} is complete (provider: {provider}).")
        return
    print_fn(f"Wrote {env_path} (provider: {provider}). Still needed before boot:")
    for gap in gaps:
        print_fn(f"  - {gap}")
    print_fn("")
    if aws_keys_blank:
        print_fn("Outside this installer:")
        print_fn("  - AWS auth: with the key pair blank, attach an EC2-launch")
        print_fn("    IAM role to this box (or fill AWS_ACCESS_KEY_ID/SECRET).")
    elif provider == "exe":
        print_fn("Outside this installer:")
        print_fn("  - Make sure tailscaled is up (`tailscale status`).")
    print_fn("Then re-run the installer.")

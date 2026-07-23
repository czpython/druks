import base64
import logging
from pathlib import Path
from typing import Annotated, Any, Literal

import asyncssh
from pydantic import BeforeValidator, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_DATA_DIR = Path("/var/lib/druks")

# The MCP default-server catalog Druks ships (Linear declared but disabled);
# ``mcp_catalog_path`` points a deployment at its own file instead.
PACKAGED_MCP_CATALOG = Path(__file__).with_name("mcp") / "catalog.json"

# The trust pins for the MCP registry picker's official badge (see
# druks/mcp/registry.py); ``mcp_trusted_path`` points a deployment at its
# own file instead.
PACKAGED_MCP_TRUSTED = Path(__file__).with_name("mcp") / "trusted.json"


def _empty_str_to_none(value: Any) -> Any:
    if value == "":
        return None
    return value


def _expand_path(value: Any) -> Any:
    if isinstance(value, str):
        return Path(value).expanduser()
    if isinstance(value, Path):
        return value.expanduser()
    return value


def _expand_optional_path(value: Any) -> Any:
    if value in (None, ""):
        return None
    return _expand_path(value)


def _secrets_key(value: Any) -> Any:
    # Comma-separated base64 32-byte master keys: the first encrypts, every
    # key decrypts — rotation is prepending a fresh key, stored rows keep
    # decrypting under the old one. Blank segments are config noise, dropped;
    # none left, or a malformed one, refuses boot: a keyless process could
    # neither store nor use a secret.
    segments = [segment.strip() for segment in str(value).split(",") if segment.strip()]
    if not segments:
        raise ValueError("set at least one base64-encoded 32-byte key")
    for segment in segments:
        try:
            key = base64.b64decode(segment, validate=True)
        except ValueError as error:
            raise ValueError("keys must be base64-encoded") from error
        if len(key) != 32:
            raise ValueError("keys must decode to 32 bytes")
    return ",".join(segments)


EmptyToNone = Annotated[str | None, BeforeValidator(_empty_str_to_none)]
SecretsKey = Annotated[str, BeforeValidator(_secrets_key)]
ExpandedPath = Annotated[Path, BeforeValidator(_expand_path)]
OptionalExpandedPath = Annotated[Path | None, BeforeValidator(_expand_optional_path)]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        populate_by_name=True,
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        # Frozen so accidental ``settings.field = ...`` raises rather than
        # silently mutating shared state.
        frozen=True,
        # Validation errors surface in boot logs and doctor output; a bad
        # DRUKS_SECRETS_KEY (or any secret-bearing field) must not echo its
        # value there.
        hide_input_in_errors=True,
    )

    # ``data_dir`` is the root for run artifacts and logs (via computed
    # properties below).
    data_dir: ExpandedPath = Field(default=DEFAULT_DATA_DIR, alias="DRUKS_DATA_DIR")

    # Postgres connection URL. Every engine factory and Alembic read this.
    database_url: str = Field(
        default="postgresql+psycopg://druks:druks@localhost:5432/druks",
        alias="DRUKS_DATABASE_URL",
    )

    # How a browser request resolves an account. ``none``: no authentication —
    # loopback-only deployments with a single operator account. ``header``: the
    # edge (exe.dev, Teleport, Cloudflare Access, …) authenticates and asserts
    # the operator's email in ``auth_header``; druks maps it to an account.
    # ``jwt``: the edge asserts a signed JWT in ``auth_header`` instead; druks
    # verifies it against the JWKS below and maps its identity claim. Bearer
    # personal access tokens resolve first in every mode.
    auth_mode: Literal["none", "header", "jwt"] = Field(default="none", alias="DRUKS_AUTH_MODE")
    # No default: the operator names their edge's header explicitly — druks
    # blesses no provider.
    auth_header: str = Field(default="", alias="DRUKS_AUTH_HEADER")
    auth_jwks_url: str = Field(default="", alias="DRUKS_AUTH_JWKS_URL")
    auth_jwt_issuer: str = Field(default="", alias="DRUKS_AUTH_JWT_ISSUER")
    auth_jwt_audience: str = Field(default="", alias="DRUKS_AUTH_JWT_AUDIENCE")
    auth_jwt_identity_claim: str = Field(default="email", alias="DRUKS_AUTH_JWT_IDENTITY_CLAIM")

    webhook_secret: str = Field(default="", alias="DRUKS_WEBHOOK_SECRET")
    # Public hostname webhook senders POST to (Caddy serves it; see
    # deploy/compose.yaml). Druks itself only reads it for the doctor's
    # ingress probe — empty when the edge carries webhooks some other way.
    webhook_host: str = Field(default="", alias="DRUKS_WEBHOOK_HOST")
    # The base URL the operator's browser reaches druks at (the dashboard host,
    # not the webhook ingress). The OAuth connect flow builds its callback
    # redirect from it; empty disables connecting OAuth MCP servers, loudly.
    endpoint: str = Field(default="", alias="DRUKS_ENDPOINT")
    # Encrypts stored secrets (MCP tokens, OAuth grants) at rest. Required —
    # a missing or malformed key refuses boot; `druks setup` generates one.
    secrets_key: SecretsKey = Field(alias="DRUKS_SECRETS_KEY")
    # Where druks may act is the operator Extension's installation set — GitHub's
    # own state: webhooks only arrive from installations, tokens only mint
    # for them. See GitHubClient.list_installation_accounts.

    github_api_url: str = Field(default="https://api.github.com", alias="GITHUB_API_URL")
    github_operator_app_id: EmptyToNone = Field(default=None, alias="GITHUB_OPERATOR_APP_ID")
    github_operator_private_key_path: OptionalExpandedPath = Field(  # type: ignore[assignment]
        default=None,
        alias="GITHUB_OPERATOR_PRIVATE_KEY_PATH",
    )
    github_reviewer_app_id: EmptyToNone = Field(default=None, alias="GITHUB_REVIEWER_APP_ID")
    github_reviewer_private_key_path: OptionalExpandedPath = Field(  # type: ignore[assignment]
        default=None,
        alias="GITHUB_REVIEWER_PRIVATE_KEY_PATH",
    )

    linear_webhook_secret: str = Field(default="", alias="LINEAR_WEBHOOK_SECRET")
    linear_api_key: EmptyToNone = Field(default=None, alias="LINEAR_API_KEY")

    # Jira Cloud (the second ticketing provider). All three are required to
    # enable the Jira tracker; the webhook secret gates inbound deliveries.
    jira_base_url: EmptyToNone = Field(default=None, alias="JIRA_BASE_URL")
    jira_email: EmptyToNone = Field(default=None, alias="JIRA_EMAIL")
    jira_api_token: EmptyToNone = Field(default=None, alias="JIRA_API_TOKEN")
    jira_webhook_secret: str = Field(default="", alias="JIRA_WEBHOOK_SECRET")

    # The Slack app's signing secret, gating inbound interactivity callbacks —
    # distinct from the per-destination outbound webhook URLs.
    slack_signing_secret: str = Field(default="", alias="SLACK_SIGNING_SECRET")

    redis_url: str = Field(default="redis://127.0.0.1:6379/0", alias="DRUKS_REDIS_URL")
    # Connection details for clawhaven-sandbox-service. Druks calls
    # ``POST /hosts`` per agent run to provision a VM, then SSHes in
    # over Tailscale to execute the CLI inside it. See
    # ``docs/design/sandboxed-execution.md`` for the full architecture.
    #
    # An empty URL disables sandbox-backed execution; workflows that call an
    # agent then fail when they try to acquire a host.
    sandbox_service_url: str = Field(default="", alias="DRUKS_SANDBOX_SERVICE_URL")
    sandbox_service_token: str = Field(default="", alias="DRUKS_SANDBOX_SERVICE_TOKEN")
    # Sized for the slowest provisioner.
    sandbox_service_timeout: float = Field(
        default=180.0,
        alias="DRUKS_SANDBOX_SERVICE_TIMEOUT",
    )
    # Empty → drukbox decides.
    sandbox_image: str = Field(default="", alias="DRUKS_SANDBOX_IMAGE")
    # Per-VM SSH keys when drukbox returns them; empty otherwise.
    sandbox_keys_dir: ExpandedPath = Field(
        default=DEFAULT_DATA_DIR / "sandbox-keys",
        alias="DRUKS_SANDBOX_KEYS_DIR",
    )
    # Local CLI config each harness carries into a VM at push time: Claude's
    # ``settings.json`` + plugin state (plus ``.claude.json`` beside the dir),
    # Codex's ``config.toml`` / ``AGENTS.md`` / MCP ``.credentials.json``, and
    # each CLI's ``skills`` subdir when DRUKS_SKILLS_DIR is unset. Subscription
    # auth never reads these — credentials live in the DB, written by the
    # connect flow (Settings → Harnesses). Empty => carry no local config for
    # that CLI.
    claude_config_dir: OptionalExpandedPath = Field(  # type: ignore[assignment]
        default=Path("~/.claude"),
        alias="DRUKS_CLAUDE_CONFIG_DIR",
    )
    codex_config_dir: OptionalExpandedPath = Field(  # type: ignore[assignment]
        default=Path("~/.codex"),
        alias="DRUKS_CODEX_CONFIG_DIR",
    )
    # Canonical shared-skills directory Druks pushes into every VM, at both
    # ``~/.claude/skills`` and ``~/.codex/skills`` (the CLIs read their own
    # path; the content is one shared set). Centralizing here means skills
    # are curated once on the Druks host instead of duplicated per-repo or
    # baked per-image. Must hold REAL skill dirs, not symlinks to a path
    # outside the deploy mount — the push tars with symlink-follow, so
    # cross-mount symlink targets would be unreadable in the container and
    # silently dropped. ``None`` => fall back to the per-CLI ``skills``
    # subdir of each credentials home (local-dev fallback).
    sandbox_skills_dir: OptionalExpandedPath = Field(  # type: ignore[assignment]
        default=None,
        alias="DRUKS_SKILLS_DIR",
    )
    # The MCP default-server catalog the app mounts at startup; a deployment
    # points this at its own mounted file to declare default servers (no
    # secrets in the file — see druks/mcp/catalog.py).
    mcp_catalog_path: ExpandedPath = Field(
        default=PACKAGED_MCP_CATALOG,
        alias="DRUKS_MCP_CATALOG",
    )
    # The trust-pins file the registry picker's official badge reads; a
    # deployment can point this at its own curated file.
    mcp_trusted_path: ExpandedPath = Field(
        default=PACKAGED_MCP_TRUSTED,
        alias="DRUKS_MCP_TRUSTED",
    )

    log_level: str = Field(default="INFO", alias="DRUKS_LOG_LEVEL")

    @model_validator(mode="after")
    def _auth_mode_is_fully_configured(self) -> "Settings":
        if self.auth_mode != "none" and not self.auth_header.strip():
            raise ValueError(
                "DRUKS_AUTH_HEADER must name the edge's identity header "
                f"when DRUKS_AUTH_MODE={self.auth_mode}"
            )
        if self.auth_mode != "none" and self.auth_header.strip().lower() == "authorization":
            # Authorization is the PAT slot and always parses bearer-first — an
            # assertion configured there could never be read, locking everyone out.
            raise ValueError("DRUKS_AUTH_HEADER cannot be Authorization — that slot is PAT-only")
        if self.auth_mode == "jwt":
            required = {
                "DRUKS_AUTH_JWKS_URL": self.auth_jwks_url,
                "DRUKS_AUTH_JWT_ISSUER": self.auth_jwt_issuer,
                "DRUKS_AUTH_JWT_AUDIENCE": self.auth_jwt_audience,
                "DRUKS_AUTH_JWT_IDENTITY_CLAIM": self.auth_jwt_identity_claim,
            }
            missing = [name for name, value in required.items() if not value.strip()]
            if missing:
                raise ValueError(f"DRUKS_AUTH_MODE=jwt requires {', '.join(missing)}")
        return self

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def artifacts_dir(self) -> Path:
        return self.data_dir / "artifacts"

    @property
    def skills_dir(self) -> Path:
        # Operator-installed skills, pushed into every VM. Defaults to a writable
        # dir under ``data_dir`` (the UI installs into it); an explicit
        # ``DRUKS_SKILLS_DIR`` still overrides for external trees.
        return self.sandbox_skills_dir or (self.data_dir / "skills")


def load_settings() -> Settings:
    return Settings()


def setup_logging(settings: Settings) -> None:
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    # asyncssh logs every channel open/close/exit and every sftp client
    # start/exit at INFO. A single sandbox operation opens dozens of channels
    # and emits ~6 lines each — drowns the worker log otherwise. The
    # library exposes a first-party knob (``set_log_level`` /
    # ``set_sftp_log_level``); ``NOTSET`` would track the root logger,
    # so we explicitly gate to WARNING. Real failures (auth, connection
    # drops) stay audible.
    asyncssh.set_log_level(logging.WARNING)
    asyncssh.set_sftp_log_level(logging.WARNING)

    if not settings.webhook_secret:
        logging.getLogger(__name__).warning(
            "DRUKS_WEBHOOK_SECRET is not set — all webhooks will be rejected.",
        )


def ensure_data_dirs(settings: Settings) -> None:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.logs_dir.mkdir(parents=True, exist_ok=True)
    settings.artifacts_dir.mkdir(parents=True, exist_ok=True)
    settings.skills_dir.mkdir(parents=True, exist_ok=True)

import logging
import os
from pathlib import Path
from typing import Annotated, Any, Literal

import asyncssh
from jsonpointer import JsonPointer, JsonPointerException
from pydantic import AfterValidator, BaseModel, BeforeValidator, Field, model_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)
from sqlalchemy_encrypted_field import validate_keys

from druks.core.utils.time import validate_timezone

DEFAULT_DATA_DIR = Path("/var/lib/druks")

# The empty MCP catalog Druks ships. Startup loads ``mcp_catalog_path``
# unconditionally, so the file stays even though it declares nothing.
PACKAGED_MCP_CATALOG = Path(__file__).with_name("mcp") / "catalog.json"

# Trust pins for the registry picker's official badge; ``mcp_trusted_path``
# points a deployment at its own file.
PACKAGED_MCP_TRUSTED = Path(__file__).with_name("mcp") / "trusted.json"


def _expand_path(value: Any) -> Any:
    if isinstance(value, str):
        return Path(value).expanduser()
    if isinstance(value, Path):
        return value.expanduser()
    return value


def _expand_optional_path(value: Any) -> Any:
    return _expand_path(value) if value else None


SecretsKey = Annotated[str, BeforeValidator(validate_keys)]
ExpandedPath = Annotated[Path, BeforeValidator(_expand_path)]
OptionalExpandedPath = Annotated[Path | None, BeforeValidator(_expand_optional_path)]


def _config_path() -> Path | None:
    if configured := os.environ.get("DRUKS_CONFIG"):
        path = Path(configured).expanduser()
        if path.is_file():
            return path
        raise ValueError(f"DRUKS_CONFIG is not a file: {path}")
    default = Path("druks.toml")
    return default if default.is_file() else None


class _PrunedTomlSource(TomlConfigSettingsSource):
    def __call__(self) -> dict[str, Any]:
        def drop_blank_values(value: Any) -> Any:
            if isinstance(value, dict):
                return {key: drop_blank_values(item) for key, item in value.items() if item != ""}
            return value

        return drop_blank_values(super().__call__())


class Identity(BaseModel):
    # ``none``: no authentication. ``header``: the edge asserts an email in
    # ``identity.header``; ``jwt``: a signed JWT there. Bearer tokens resolve first.
    mode: Literal["none", "header", "jwt"] = "none"
    # No default: the operator names their edge's header explicitly — druks
    # blesses no provider.
    header: str = ""
    jwks_url: str = ""
    jwt_issuer: str = ""
    jwt_audience: str = ""
    jwt_identity_claim: str = "/email"

    @model_validator(mode="after")
    def _auth_mode_is_fully_configured(self) -> "Identity":
        if self.mode != "none" and not self.header.strip():
            raise ValueError(
                "identity.header must name the edge's identity header "
                f"when identity.mode={self.mode}"
            )
        if self.mode != "none" and self.header.strip().lower() == "authorization":
            # Authorization is the PAT slot and always parses bearer-first — an
            # assertion configured there could never be read, locking everyone out.
            raise ValueError("identity.header cannot be Authorization — that slot is PAT-only")
        if self.mode == "jwt":
            required = {
                "identity.jwks_url": self.jwks_url,
                "identity.jwt_issuer": self.jwt_issuer,
                "identity.jwt_audience": self.jwt_audience,
                "identity.jwt_identity_claim": self.jwt_identity_claim,
            }
            missing = [name for name, value in required.items() if not value.strip()]
            if missing:
                raise ValueError(f"identity.mode=jwt requires {', '.join(missing)}")
            try:
                JsonPointer(self.jwt_identity_claim)
            except JsonPointerException as error:
                raise ValueError(
                    "identity.jwt_identity_claim must be a JSON Pointer, such as /email"
                ) from error
        return self


class Urls(BaseModel):
    # The dashboard base URL. OAuth connect callbacks build on it; empty
    # disables connecting OAuth MCP servers, loudly.
    endpoint: str = ""
    # The public webhook hostname Caddy serves. Druks reads it only for the
    # doctor's ingress probe.
    webhook_host: str = ""


class Secrets(BaseModel):
    # Encrypts stored secrets at rest. A missing or malformed key refuses boot;
    # `druks setup` generates one.
    secrets_key: SecretsKey


class Sandbox(BaseModel):
    # The drukbox control plane. An empty service_url turns sandbox execution off.
    service_url: str = ""
    service_token: str = ""
    # Empty → drukbox decides.
    image: str = ""
    # The issuer base URL the secrets exchange dials: the web process on the
    # host loopback, or the Caddy issuer listener for a drukbox on another server.
    issuer_url: str = "http://127.0.0.1:8001"
    # The browser home: browser containers boot on this provider with this image.
    browser_sandbox_provider: str = "docker"
    browser_sandbox_image: str = "ghcr.io/czpython/druks/browser:latest"
    # An HTTP proxy for the login window only, so the login leaves from another
    # IP. It may carry a user name and password. Empty keeps the box IP.
    browser_login_proxy: str = ""
    # The IANA timezone of the login window, in the login proxy's region. Empty
    # keeps the container default.
    browser_login_tz: str = ""
    # Sized for the slowest provisioner.
    timeout: float = 180.0


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        populate_by_name=True,
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        # Frozen so accidental ``settings.field = ...`` raises rather than
        # silently mutating shared state.
        frozen=True,
        # A bad secret-bearing field must not echo its value in boot logs or
        # doctor output.
        hide_input_in_errors=True,
    )

    timezone: Annotated[str, AfterValidator(validate_timezone)] = "UTC"
    identity: Identity = Identity()
    urls: Urls = Urls()
    secrets: Secrets
    sandbox: Sandbox = Sandbox()

    # ``data_dir`` is the root for files, run artifacts, and logs (via computed
    # properties below).
    data_dir: ExpandedPath = Field(default=DEFAULT_DATA_DIR, alias="DRUKS_DATA_DIR")

    # Postgres connection URL. Every engine factory and Alembic read this.
    database_url: str = Field(
        default="postgresql+psycopg://druks:druks@localhost:5432/druks",
        alias="DRUKS_DATABASE_URL",
    )

    # Transport only — GitHub credentials live on the service-identity row,
    # not in Settings; this points every client at a compatible API endpoint.
    github_api_url: str = Field(default="https://api.github.com", alias="GITHUB_API_URL")

    # The Slack app's signing secret, gating inbound interactivity callbacks —
    # distinct from the per-destination outbound webhook URLs.
    slack_signing_secret: str = Field(default="", alias="SLACK_SIGNING_SECRET")

    redis_url: str = Field(default="redis://127.0.0.1:6379/0", alias="DRUKS_REDIS_URL")
    # Per-VM SSH keys when drukbox returns them; empty otherwise.
    sandbox_keys_dir: ExpandedPath = Field(
        default=DEFAULT_DATA_DIR / "sandbox-keys",
        alias="DRUKS_SANDBOX_KEYS_DIR",
    )
    # Each harness owns one directory under this root. Druks copies only the
    # files that harness declares. Provider credentials come from the database.
    harness_config_root: ExpandedPath = Field(
        default=Path("~/.config/druks/harnesses"),
        alias="DRUKS_HARNESS_CONFIG_ROOT",
    )
    # Shared skills pushed into every VM. Real directories only: the push
    # follows symlinks and drops a target outside the mount.
    sandbox_skills_dir: OptionalExpandedPath = Field(  # type: ignore[assignment]
        default=None,
        alias="DRUKS_SKILLS_DIR",
    )
    # The MCP default-server catalog loaded at startup. No secrets in the file
    # (see druks/mcp/catalog.py).
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

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (
            init_settings,
            _PrunedTomlSource(settings_cls, toml_file=_config_path()),
            env_settings,
            dotenv_settings,
        )

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def artifacts_dir(self) -> Path:
        return self.data_dir / "artifacts"

    @property
    def files_dir(self) -> Path:
        return self.data_dir / "files"

    @property
    def skills_dir(self) -> Path:
        # Operator-installed skills, pushed into every VM. ``DRUKS_SKILLS_DIR``
        # overrides the writable default under ``data_dir``.
        return self.sandbox_skills_dir or (self.data_dir / "skills")


def load_settings() -> Settings:
    return Settings()  # pyright: ignore[reportCallIssue]


def setup_logging(settings: Settings) -> None:
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    # asyncssh logs every channel and sftp event at INFO, dozens per sandbox
    # operation. Real failures stay audible at WARNING.
    asyncssh.set_log_level(logging.WARNING)
    asyncssh.set_sftp_log_level(logging.WARNING)


def ensure_data_dirs(settings: Settings) -> None:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.logs_dir.mkdir(parents=True, exist_ok=True)
    settings.artifacts_dir.mkdir(parents=True, exist_ok=True)
    settings.files_dir.mkdir(parents=True, exist_ok=True)
    settings.skills_dir.mkdir(parents=True, exist_ok=True)

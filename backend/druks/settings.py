import logging
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Any, Literal

import asyncssh
from jsonpointer import JsonPointer, JsonPointerException
from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    Field,
    SecretStr,
    model_validator,
)
from pydantic_settings import (
    BaseSettings,
    DotEnvSettingsSource,
    EnvSettingsSource,
    NestedSecretsSettingsSource,
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

# The MCP servers the dashboard offers to add; ``mcp_directory_path`` points a
# deployment at its own file.
PACKAGED_MCP_DIRECTORY = Path(__file__).with_name("mcp") / "directory.json"


def _expand_path(value: Any) -> Any:
    if isinstance(value, str):
        return Path(value).expanduser()
    if isinstance(value, Path):
        return value.expanduser()
    return value


def _expand_optional_path(value: Any) -> Any:
    return _expand_path(value) if value else None


SecretsKey = Annotated[SecretStr, BeforeValidator(validate_keys)]
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


def _secrets_dir() -> Path | None:
    if configured := os.environ.get("DRUKS_SECRETS_DIR"):
        path = Path(configured).expanduser()
        if path.is_dir():
            return path
        raise ValueError(f"DRUKS_SECRETS_DIR is not a directory: {path}")


def secret_variable(key: str) -> str:
    """The environment variable of the secret ``key``: DRUKS_ and the key path, with
    ``_`` for each dot."""
    return f"DRUKS_{key.replace('.', '_').upper()}"


def _pick(values: dict[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    """The dotted ``keys`` that ``values`` sets, in the nested shape of ``values``."""
    picked: dict[str, Any] = {}
    for key in keys:
        table, _, name = key.rpartition(".")
        scope = values.get(table) if table else values
        if isinstance(scope, dict) and name in scope:
            target = picked.setdefault(table, {}) if table else picked
            target[name] = scope[name]
    return picked


class _TomlSource(TomlConfigSettingsSource):
    def __call__(self) -> dict[str, Any]:
        def drop_blank_values(value: Any) -> Any:
            if isinstance(value, dict):
                return {key: drop_blank_values(item) for key, item in value.items() if item != ""}
            return value

        document = drop_blank_values(super().__call__())
        for key in Settings.secret_keys():
            if _pick(document, (key,)):
                raise ValueError(
                    f"druks.toml: {key} is a secret. Set {secret_variable(key)} in the "
                    f"environment, or write the file {key} in DRUKS_SECRETS_DIR."
                )
        for name, field in Settings.model_fields.items():
            if field.alias and (name in document or field.alias in document):
                raise ValueError(
                    f"druks.toml: {name} is not a druks.toml key. "
                    f"Set {field.alias} in the environment."
                )
        if "managed_by" in document:
            raise ValueError("druks.toml: managed_by is now the [manager] table.")
        return document


def _pick_injected(values: dict[str, Any], variables: Mapping[str, str | None]) -> dict[str, Any]:
    """What the environment sets: the settings that carry an alias, and each secret
    under the name that ``secret_variable`` builds from its key. A secret in both
    places is refused, so neither one overrides."""
    aliases = {field.alias for field in Settings.model_fields.values() if field.alias}
    secrets: dict[str, Any] = {}
    secrets_dir = _secrets_dir()
    for key in Settings.secret_keys():
        # The sources hold each variable name in lowercase.
        variable = secret_variable(key).lower()
        if variable in variables:
            if secrets_dir and (secrets_dir / key).is_file():
                raise ValueError(
                    f"{key} is set in the environment and in a secret file. Remove one of them."
                )
            table, _, name = key.rpartition(".")
            target = secrets.setdefault(table, {}) if table else secrets
            target[name] = variables[variable]
    return {key: value for key, value in values.items() if key in aliases} | secrets


class _EnvSource(EnvSettingsSource):
    def __call__(self) -> dict[str, Any]:
        return _pick_injected(super().__call__(), self.env_vars)


class _DotEnvSource(DotEnvSettingsSource):
    def __call__(self) -> dict[str, Any]:
        return _pick_injected(super().__call__(), self.env_vars)


class _SecretsSource(NestedSecretsSettingsSource):
    def __call__(self) -> dict[str, Any]:
        # Each file in the table of a configured service is a secret of its card.
        tables = tuple(f"services.{slug}" for slug in self.current_state.get("services", {}))
        return _pick(super().__call__(), Settings.secret_keys() + tables)


class Manager(BaseModel):
    """Who manages the services: it issues their tokens and signs the events it forwards."""

    name: str = ""
    jwks_url: str = ""
    issuer: str = ""
    audience: str = ""
    # The services it manages. Each one's table gives the ``url`` where it answers.
    services: list[str] = []
    # The credential of the manager's token exchange: the secret file ``manager.token``.
    token: SecretStr = SecretStr("")

    @model_validator(mode="after")
    def _is_fully_configured(self) -> "Manager":
        values = {
            "manager.name": self.name,
            "manager.jwks_url": self.jwks_url,
            "manager.issuer": self.issuer,
            "manager.audience": self.audience,
            "manager.services": ", ".join(self.services),
            "manager.token": self.token.get_secret_value(),
        }
        missing = [name for name, value in values.items() if not value.strip()]
        if missing and len(missing) < len(values):
            raise ValueError(f"[manager] requires {', '.join(missing)}")
        return self


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
    # The public webhook hostname Caddy serves.
    webhook_host: str = ""

    @property
    def webhook_base(self) -> str:
        """The base URL that providers and sandboxes reach: the webhook host, else the endpoint."""
        if self.webhook_host:
            return f"https://{self.webhook_host}"
        return self.endpoint.rstrip("/")


class Sandbox(BaseModel):
    # The drukbox control plane. An empty service_url turns sandbox execution off.
    service_url: str = ""
    service_token: SecretStr = SecretStr("")
    # Empty → drukbox decides.
    image: str = ""
    # The issuer base URL the secrets exchange dials: the web process on the
    # host loopback, or the Caddy issuer listener for a drukbox on another server.
    issuer_url: str = "http://127.0.0.1:8001"
    # Sized for the slowest provisioner.
    timeout: float = 180.0


class Browser(BaseModel):
    # The browser home: browser containers boot on this provider with this image.
    sandbox_provider: str = "docker"
    sandbox_image: str = "ghcr.io/czpython/druks/browser:latest"
    # An HTTP proxy the browser leaves through instead of the box IP. It may
    # carry a user name and password. Empty means no proxy anywhere.
    proxy: SecretStr = SecretStr("")
    # ``login``: only the login window uses the proxy. ``all``: the login window
    # and every borrow use it.
    proxy_scope: Literal["login", "all"] = "login"
    # The IANA timezone of the proxied browser, in the proxy's region. Empty
    # keeps the container default.
    timezone: str = ""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        populate_by_name=True,
        env_prefix="DRUKS_",
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
    # The Services page names the manager on each managed card.
    manager: Manager = Manager()
    identity: Identity = Identity()
    urls: Urls = Urls()
    sandbox: Sandbox = Sandbox()
    browser: Browser = Browser()
    # A [services.<slug>] table makes the card of that service managed. Each table holds
    # its secrets as plain text, so no output shows the field.
    services: dict[str, dict] = Field(default={}, repr=False, exclude=True)

    # Encrypts stored secrets at rest. A missing or malformed key refuses boot;
    # `druks setup` generates one.
    secrets_key: SecretsKey

    # ``data_dir`` is the root for files, run artifacts, and logs (via computed
    # properties below).
    data_dir: ExpandedPath = Field(default=DEFAULT_DATA_DIR, alias="DRUKS_DATA_DIR")

    # Postgres connection URL. Every engine factory and Alembic read this.
    database_url: SecretStr = SecretStr("postgresql+psycopg://druks:druks@localhost:5432/druks")
    # SQLAlchemy reads a pool size of 0 as unbounded.
    database_pool_size: int = Field(default=20, gt=0, alias="DRUKS_DATABASE_POOL_SIZE")
    database_max_overflow: int = Field(default=30, ge=0, alias="DRUKS_DATABASE_MAX_OVERFLOW")
    dbos_pool_size: int = Field(default=20, gt=0, alias="DRUKS_DBOS_POOL_SIZE")

    # A secret, because the URL can carry the Redis password.
    redis_url: SecretStr = SecretStr("redis://127.0.0.1:6379/0")
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
    # The MCP servers the dashboard offers to add; a deployment can point this
    # at its own curated file.
    mcp_directory_path: ExpandedPath = Field(
        default=PACKAGED_MCP_DIRECTORY,
        alias="DRUKS_MCP_DIRECTORY",
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
        """Each setting has one source. druks.toml sets ``timezone`` and the tables. The
        environment sets the settings that carry an alias. A secret comes from the
        environment, or from its file when DRUKS_SECRETS_DIR names a directory."""
        return (
            init_settings,
            _TomlSource(settings_cls, toml_file=_config_path()),
            _EnvSource(settings_cls),
            _DotEnvSource(settings_cls),
            _SecretsSource(
                file_secret_settings,
                secrets_dir=_secrets_dir(),
                secrets_nested_delimiter=".",
                secrets_prefix="",
            ),
        )

    @classmethod
    def secret_keys(cls) -> tuple[str, ...]:
        """The dotted key of each secret: its file name in DRUKS_SECRETS_DIR."""
        keys: list[str] = []
        for name, field in cls.model_fields.items():
            if field.annotation is SecretStr:
                keys.append(name)
            elif isinstance(field.annotation, type) and issubclass(field.annotation, BaseModel):
                keys.extend(
                    f"{name}.{key}"
                    for key, table_field in field.annotation.model_fields.items()
                    if table_field.annotation is SecretStr
                )
        return tuple(keys)

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

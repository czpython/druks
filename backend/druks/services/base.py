import re
from dataclasses import dataclass
from datetime import datetime
from types import SimpleNamespace
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, ValidationError, create_model
from sqlalchemy.ext.asyncio import AsyncSession

from druks.apps.base import NAME_RE
from druks.apps.loader import iter_apps
from druks.apps.registry import services
from druks.apps.settings import field_kind, field_multiline
from druks.db import db_session
from druks.mcp.models import McpServer
from druks.secrets.datastructures import Audience
from druks.secrets.enums import SecretKind
from druks.secrets.models import VaultSecret
from druks.settings import load_settings

from .exceptions import (
    OauthExchangeError,
    ServiceConnectError,
    ServiceManagedError,
    ServiceNotConnectedError,
)
from .oauth import OauthClient, fetch_identity, is_grant_revoked

# GoogleCalendar -> google_calendar, HTTPServer -> http_server.
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


class Connection:
    """One signed-in provider account. ``get_access_token`` and ``disconnect``
    act on this sign-in only."""

    def __init__(self, service: "type[Service]", row: VaultSecret) -> None:
        self.service = service
        self.row = row

    @property
    def id(self) -> str:
        return self.row.id

    @property
    def scopes(self) -> list[str] | None:
        return self.row.scopes

    @property
    def identity(self) -> dict[str, Any]:
        return self.row.identity

    @property
    def account_id(self) -> str | None:
        return self.row.account_id

    @property
    def connected_at(self):
        return self.row.updated_at

    async def get_access_token(self, scopes: tuple[str, ...] = (), cached: bool = True) -> str:
        client = await self.service.get_oauth_client()
        token, _ = await client.get_access_token(
            db_session(), connection=self.row, scopes=scopes, cached=cached
        )
        return token

    async def disconnect(self) -> None:
        await OauthClient(provider=self.service.slug).disconnect(self.row, reason="user")


@dataclass(frozen=True)
class ServiceField:
    """One field of a service's ``Settings``, named before any value exists:
    ``Acme.fields.api_key``. An agent lists a secret field in ``secrets`` to
    hold it in its sandbox."""

    service: "type[Service]"
    name: str

    @property
    def is_secret(self) -> bool:
        return field_kind(self.service.settings_model.model_fields[self.name]) == "secret"

    @property
    def secret_name(self) -> str:
        """The sandbox's name for the secret. Its variable is this name in upper
        case, and the issuer reads the field back out of it."""
        return f"{self.service.slug}_{self.name}"


class ScopedService:
    """A service seen through one app's declared scopes
    (``gmail = Gmail.with_scopes("gmail.readonly")``). The declaration
    feeds the consent union; the handle reads the connections that grant
    it."""

    def __init__(self, service: "type[Service]", scopes: tuple[str, ...]) -> None:
        self.service = service
        self.scopes = scopes

    def __set_name__(self, owner: type, name: str) -> None:
        self.owner = owner
        self.name = name

    @property
    def label(self) -> str:
        return f"{self.owner.name}.{self.name}"

    @property
    def connect_url(self) -> str:
        """Where an operator connects an account to this service. The provider
        sends them back to the app."""
        return f"/api/oauth/{self.service.slug}/connect?next=/{self.owner.name}"

    async def list_for_account(self, account_id: str) -> list[Connection]:
        return await self.service.list_for_account(account_id)

    async def get(self, connection_id: str) -> Connection | None:
        row = await db_session().get(VaultSecret, connection_id)
        if (
            row
            and row.kind == SecretKind.OAUTH
            and row.audience == Audience.service(self.service.slug)
            and not row.revoked_at
        ):
            return Connection(self.service, row)


class Service:
    """The appliance's own identity at an external provider — one per service,
    declared by the code that consumes it. Subclass in a ``services`` module
    with an inner ``Settings`` model; the platform renders the connect card,
    verifies and stores the paste, and reports doctor state, all from the
    declaration. Read back through the same class:
    ``Gmail.get().secrets["client_secret"]``.

    Used as a class, never instantiated — the same install-singleton shape as
    ``App``.
    """

    # Keys the service_identities row and the connect wire. Druks derives it
    # from the class name. Set it only to keep the key after a class rename.
    slug: ClassVar[str]
    # The connect card's heading. Druks derives it from the slug.
    title: ClassVar[str]
    description: ClassVar[str] = ""
    # Whether doctor fails when this service is not connected.
    required: ClassVar[bool] = True
    # What the connected credential is in the vault: a pasted value, or an
    # App key that issues tokens.
    secret_kind: ClassVar[SecretKind] = SecretKind.STATIC
    # True marks a shared provider base. It never registers; its subclasses do.
    abstract: ClassVar[bool] = False
    settings_model: ClassVar[type[BaseModel]]
    # The ``Settings`` fields by name, each a ``ServiceField``.
    fields: ClassVar[SimpleNamespace]
    # The one host a sandbox may send this service's secrets to. The secrets
    # proxy swaps a placeholder for the secret on requests to it only.
    host: ClassVar[str] = ""
    # The host of the MCP server that this identity also signs in to. That
    # server uses the service's credential instead of registering its own client.
    mcp_host: ClassVar[str] = ""
    # Set both endpoints when the registered app is an OAuth client;
    # ``get_oauth_client()`` then hands back the connected identity as a
    # configured ``OauthClient``. Scopes are not declared here — the
    # apps that use the service declare them (``with_scopes``), and
    # the connect door asks for their union.
    authorization_endpoint: ClassVar[str] = ""
    token_endpoint: ClassVar[str] = ""
    # HTTP Basic on the token endpoint; False sends the secret in the body.
    basic_auth: ClassVar[bool] = False
    # The provider's consent-query quirks — Google grants a refresh token
    # only with access_type=offline and prompt=consent.
    extra_authorize_params: ClassVar[dict[str, str]] = {}
    # The endpoint that returns the signed-in account's facts;
    # identity_scopes join the consent ask.
    identity_endpoint: ClassVar[str] = ""
    identity_scopes: ClassVar[tuple[str, ...]] = ()
    # The identity fact that names the provider account — "sub" for Google,
    # "subject" for GitHub. When set, a fresh sign-in that matches an existing
    # connection for the same owner updates that row; a revoked row becomes
    # live again. When empty, each fresh sign-in creates a new connection.
    identity_key: ClassVar[str] = ""
    # The app that the create page registers at the provider. A service with one
    # offers that page on its card; without one, the person pastes credentials.
    manifest: ClassVar[dict[str, Any]] = {}

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if "name" in cls.__dict__:
            raise TypeError(
                f"{cls.__name__} declares a `name`. A service keys by `slug`, "
                "derived from the class name. Drop `name` or set `slug`."
            )
        if "title" in cls.__dict__:
            raise TypeError(
                f"{cls.__name__} declares a `title`. Druks derives the card "
                "heading from the slug. Drop `title`."
            )
        if cls.__dict__.get("abstract"):
            if "slug" in cls.__dict__:
                raise TypeError(
                    f"{cls.__name__} sets both `abstract` and `slug`. An abstract "
                    "base never registers. Drop one."
                )
            return
        if "slug" in cls.__dict__:
            slug = cls.__dict__["slug"]
        else:
            slug = _CAMEL_BOUNDARY.sub("_", cls.__name__).lower()
        if not NAME_RE.match(slug):
            raise TypeError(
                f"service slug {slug!r} must match {NAME_RE.pattern!r}. It keys the "
                "service_identities row and the connect wire."
            )
        declared = getattr(cls, "Settings", None)
        if not isinstance(declared, type) or not issubclass(declared, BaseModel):
            raise TypeError(f"{cls.__name__}.Settings must be a pydantic model")
        if bool(cls.authorization_endpoint) != bool(cls.token_endpoint):
            raise TypeError(f"{cls.__name__} must declare both OAuth endpoints or neither")
        if cls.token_endpoint and not {"client_id", "client_secret"} <= set(declared.model_fields):
            raise TypeError(
                f"{cls.__name__}.Settings must declare client_id and client_secret "
                "fields — get_oauth_client() reads the OAuth client from them"
            )
        # A registered service is concrete even under an abstract base.
        cls.abstract = False
        cls.slug = slug
        cls.title = slug.replace("_", " ").title()
        cls.settings_model = declared
        cls.fields = SimpleNamespace(
            **{name: ServiceField(cls, name) for name in declared.model_fields}
        )
        services.register(cls)

    @classmethod
    async def verify(cls, settings: Any) -> dict[str, Any]:
        """Identity facts proven against the live provider, merged into the
        stored ``identity``. Receives an instance of the subclass's own
        ``Settings``. Raise ``ServiceConnectError`` to reject the paste; the
        default accepts it and proves nothing."""
        return {}

    @classmethod
    def connect_fields(cls) -> list[dict[str, Any]]:
        """The connect form's fields, read off ``Settings`` — what the card
        renders and the wire serves, in the settings-form field vocabulary."""
        return [
            {
                "name": name,
                "label": field.title or name,
                "help": field.description or "",
                "type": field_kind(field),
                "multiline": field_multiline(field),
                "is_required": field.is_required(),
            }
            for name, field in cls.settings_model.model_fields.items()
        ]

    @classmethod
    def get_create_url(cls) -> str:
        """The page that creates the provider's app from ``manifest`` and connects it."""
        return f"/api/core/services/{cls.slug}/manifest"

    @classmethod
    async def get(cls) -> VaultSecret:
        """The connected identity's vault row, or ServiceNotConnectedError."""
        if row := await VaultSecret.lookup(
            db_session(), cls.secret_kind, Audience.service(cls.slug)
        ):
            return row
        raise ServiceNotConnectedError(cls.slug)

    @classmethod
    def is_configured(cls) -> bool:
        """Whether druks.toml configures this service. Its card then refuses a change."""
        return cls.slug in load_settings().services

    @classmethod
    def is_managed(cls) -> bool:
        """Whether the manager manages this service, as ``manager.services`` says."""
        return cls.slug in load_settings().manager.services

    @classmethod
    async def sync_configuration(cls, session: AsyncSession) -> None:
        """Store each service that druks.toml configures as its card. An entry replaces
        the credentials of a card that exists; the facts the card learned since stay."""
        configuration = load_settings()
        for slug in configuration.manager.services:
            if not services.get(slug):
                raise ServiceConnectError(
                    f"[manager] manages the service {slug!r}, and no installed app declares "
                    "it. Remove it from manager.services."
                )
        for slug, entry in configuration.services.items():
            service = services.get(slug)
            if not service:
                raise ServiceConnectError(
                    f"druks.toml configures the service {slug!r}, and no installed app "
                    f"declares it. Remove [services.{slug}]."
                )
            model = service.settings_model
            if service.is_managed():
                # The card is the plain fields, the ``url``, and the secrets the table gives;
                # any other key is a fact.
                fields = {
                    name: (field.annotation, field)
                    for name, field in model.model_fields.items()
                    if field_kind(field) != "secret" or name in entry
                }
                model = create_model(
                    model.__name__,
                    __config__=ConfigDict(extra="allow"),
                    **{"url": (str, ...), **fields},
                )
            try:
                settings = model.model_validate(entry)
            except ValidationError as error:
                invalid_fields = ", ".join(
                    f"services.{slug}.{item['loc'][0]}" for item in error.errors()
                )
                # The cause shows the values of the entry, and startup prints the traceback.
                raise ServiceConnectError(
                    f"The entry [services.{slug}] is not valid. Correct {invalid_fields}."
                ) from None
            card = await VaultSecret.lookup(session, service.secret_kind, Audience.service(slug))
            facts = card.identity if card else {}
            learned = {name: value for name, value in facts.items() if name not in entry}
            await service._store(session, settings, proven=learned)

    @classmethod
    async def _store(cls, session: AsyncSession, settings: BaseModel, proven: dict) -> VaultSecret:
        """Store the card from its validated ``settings``: secrets encrypted, the rest as
        identity facts beside the ``proven`` ones."""
        secrets = {
            name: getattr(settings, name).get_secret_value()
            for name, field in type(settings).model_fields.items()
            if field_kind(field) == "secret"
        }
        identity = {
            name: value for name, value in settings.model_dump().items() if name not in secrets
        }
        return await VaultSecret.store(
            session,
            cls.secret_kind,
            Audience.service(cls.slug),
            identity={**identity, **proven},
            secrets=secrets,
        )

    @classmethod
    def with_scopes(cls, *scopes: str) -> ScopedService:
        """Declare this app's use of the service and the scopes its
        calls need."""
        if not cls.token_endpoint:
            raise TypeError(f"{cls.__name__} declares no OAuth endpoints")
        return ScopedService(cls, scopes)

    @classmethod
    async def list_for_account(cls, account_id: str) -> list[Connection]:
        """The account's live sign-ins, without declaring an app's scope requirements."""
        return [
            Connection(cls, row)
            for row in await VaultSecret.list_account_connections(
                db_session(), Audience.service(cls.slug), account_id
            )
        ]

    @classmethod
    def declarations(cls) -> "list[ScopedService]":
        return [
            value
            for app in iter_apps()
            for value in vars(app).values()
            if isinstance(value, ScopedService) and value.service is cls
        ]

    @classmethod
    def scopes(cls) -> tuple[str, ...]:
        """The union of every installed declaration's scopes — the consent ask."""
        scopes = {scope for declaration in cls.declarations() for scope in declaration.scopes}
        scopes.update(cls.identity_scopes)
        return tuple(sorted(scopes))

    @classmethod
    def get_profile(cls, identity: dict[str, Any]) -> dict[str, Any]:
        """The facts that name the person at the provider: login, email, name. Not the
        authority or the identity key, which only match the sign-in to its account."""
        hidden = ("authority", cls.identity_key)
        return {key: value for key, value in identity.items() if key not in hidden}

    @classmethod
    async def get_identity(cls, access_token: str) -> dict[str, Any]:
        """The signed-in account's facts, read from ``identity_endpoint``.
        Override when the provider needs a different call."""
        if cls.identity_endpoint:
            return await fetch_identity(cls.identity_endpoint, access_token)
        return {}

    @classmethod
    def get_consent_query(cls, scopes: tuple[str, ...]) -> dict[str, str]:
        """The consent query that asks for ``scopes``. Override for a provider that
        names or joins them differently."""
        return {"scope": " ".join(scopes)}

    @classmethod
    def read_grant(cls, tokens: dict[str, Any]) -> dict[str, Any]:
        """The grant in the token endpoint's answer: its access token, its refresh
        token, and its scopes. Override for a provider that shapes the answer
        differently. A grant whose refresh token is empty is one access token that
        never expires."""
        if not tokens.get("refresh_token"):
            raise OauthExchangeError(
                cls.slug,
                "the authorization server granted no refresh token; druks needs offline access",
                context={},
            )
        return {
            "access_token": tokens["access_token"],
            "refresh_token": tokens["refresh_token"],
            "scopes": tokens.get("scope", "").split(),
        }

    @classmethod
    def is_grant_revoked(cls, status: int, tokens: dict[str, Any]) -> bool:
        """Whether the token endpoint's answer to a refresh says the provider revoked
        the grant. Override for a provider that reports it otherwise than RFC 6749."""
        return is_grant_revoked(status, tokens)

    @classmethod
    async def get_oauth_client(cls) -> OauthClient:
        """The connected identity as a configured ``OauthClient``, keyed by
        the service slug. Raises ``ServiceNotConnectedError`` until the
        operator connects the service."""
        if not cls.token_endpoint:
            raise TypeError(f"{cls.__name__} declares no OAuth endpoints")
        connected = await cls.get()
        if cls.is_managed():
            manager = load_settings().manager
            return OauthClient(
                provider=cls.slug,
                authorization_endpoint=cls.authorization_endpoint,
                token_endpoint=f"{connected.identity['url'].rstrip('/')}/oauth/token",
                client_id=connected.identity["client_id"],
                extra_authorize_params=cls.extra_authorize_params,
                token_headers={"Authorization": f"Bearer {manager.token.get_secret_value()}"},
                state_prefix=f"{manager.audience}.",
                redirect_uri=f"{manager.issuer.rstrip('/')}/{cls.slug}/oauth/callback",
                is_grant_revoked=cls.is_grant_revoked,
            )
        return OauthClient(
            provider=cls.slug,
            authorization_endpoint=cls.authorization_endpoint,
            token_endpoint=cls.token_endpoint,
            client_id=connected.identity["client_id"],
            client_secret=connected.secrets["client_secret"],
            basic_auth=cls.basic_auth,
            extra_authorize_params=cls.extra_authorize_params,
            is_grant_revoked=cls.is_grant_revoked,
        )

    @classmethod
    async def get_install_endpoint(cls) -> str:
        """The provider page that installs the app, then runs the sign-in flow with
        the installation in its callback. Empty for a provider without one."""
        return ""

    @classmethod
    async def sync_installations(cls) -> None:
        """Record the app's installations on the card. Runs after an install exchange."""

    @classmethod
    async def issue_token(cls, resource: str) -> tuple[str, datetime]:
        """The token a sandbox fetches for this identity, and its expiry. A
        service without one raises."""
        raise NotImplementedError(f"{cls.slug} issues no sandbox token")

    @classmethod
    def get_authorization(cls, login: VaultSecret) -> str:
        """The ``Authorization`` value of the connected row, which the default account
        sends to the server at ``mcp_host``. A service without a pasted login raises."""
        raise NotImplementedError(f"{cls.slug} has no pasted login")

    @classmethod
    async def is_connected(cls) -> bool:
        return bool(
            await VaultSecret.lookup(db_session(), cls.secret_kind, Audience.service(cls.slug))
        )

    @classmethod
    async def connect(cls, payload: dict[str, str]) -> VaultSecret:
        """Verify and store a paste of the service's fields: secrets encrypted, the rest as
        identity facts. On a connected card a blank secret keeps the stored one. Another
        client ID revokes the connections that belong to the old one."""
        if cls.is_configured():
            raise ServiceManagedError(cls.slug)
        fields = cls.settings_model.model_fields
        session = db_session()
        card = await VaultSecret.lookup(session, cls.secret_kind, Audience.service(cls.slug))
        # A card from before its service had a sign-in names no client.
        previous_client_id = card.identity.get("client_id") if card else None
        pasted = dict(card.secrets) if card else {}
        for name, value in payload.items():
            if name not in fields:
                continue
            if field_kind(fields[name]) != "secret":
                value = value.strip()
            if value:
                pasted[name] = value
        try:
            settings = cls.settings_model.model_validate(pasted)
        except ValidationError as error:
            labels = {spec["name"]: spec["label"] for spec in cls.connect_fields()}
            missing = ", ".join(labels[err["loc"][0]] for err in error.errors())
            raise ServiceConnectError(f"Enter {missing}.") from error
        row = await cls._store(session, settings, proven=await cls.verify(settings))
        if cls.token_endpoint and row.identity["client_id"] != previous_client_id:
            client = OauthClient(provider=cls.slug)
            for connection in await VaultSecret.list_connections(
                session, Audience.service(cls.slug)
            ):
                await client.disconnect(connection, reason="client_replaced")
        return row

    @classmethod
    async def disconnect(cls, session: AsyncSession) -> None:
        """Revoke the card, every account's sign-in, and the login its MCP servers derived.
        The provider keeps its application."""
        audience = Audience.service(cls.slug)
        client = OauthClient(provider=cls.slug)
        for connection in await VaultSecret.list_connections(session, audience):
            await client.disconnect(connection, reason="service_disconnected")
        if card := await VaultSecret.lookup(session, cls.secret_kind, audience):
            await card.revoke("user")
        await McpServer.remove_login(session, cls.mcp_host)

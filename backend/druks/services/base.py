import logging
import re
from dataclasses import dataclass
from datetime import datetime
from types import SimpleNamespace
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, ValidationError, create_model
from pydantic.fields import FieldInfo
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

from . import auth
from .exceptions import (
    ServiceConnectError,
    ServiceDisabledError,
    ServiceManagedError,
    ServiceNotConnectedError,
)
from .oauth import OauthClient
from .resources import Resource

logger = logging.getLogger(__name__)

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
    # How the service authenticates: its nested ``Authentication``, found by name like
    # ``Settings``, names its ways and carries the provider's behavior. See ``auth``.
    Authentication: ClassVar[type[auth.Authentication]] = auth.Authentication
    # What the connected row is in the vault: an App key when the service has a ``jwt``.
    secret_kind: ClassVar[SecretKind] = SecretKind.STATIC
    # True marks a shared provider base. It never registers; its subclasses do.
    abstract: ClassVar[bool] = False
    settings_model: ClassVar[type[BaseModel]]
    # The ``Settings`` fields by name, each a ``ServiceField``.
    fields: ClassVar[SimpleNamespace]
    resources: ClassVar[dict[str, type[Resource]]]
    # The one host a sandbox may send this service's secrets to. The secrets
    # proxy swaps a placeholder for the secret on requests to it only.
    host: ClassVar[str] = ""
    # The host of the MCP server that this identity also signs in to. That
    # server uses the service's credential instead of registering its own client.
    mcp_host: ClassVar[str] = ""
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
        if not declared:
            declared = create_model(f"{cls.__name__}Settings")
        if not isinstance(declared, type) or not issubclass(declared, BaseModel):
            raise TypeError(f"{cls.__name__}.Settings must be a pydantic model")
        authentication = cls.__dict__.get("Authentication")
        if not authentication:
            # Each service gets a class of its own, so ``service`` points at it alone.
            authentication = type("Authentication", (cls.Authentication,), {})
        if not (
            isinstance(authentication, type) and issubclass(authentication, auth.Authentication)
        ):
            raise TypeError(f"{cls.__name__}.Authentication must subclass auth.Authentication")
        for name, kind in auth.WAYS.items():
            way = getattr(authentication, name, None)
            if way and not isinstance(way, kind):
                raise TypeError(
                    f"{cls.__name__}.Authentication.{name} must be an auth.{kind.__name__}"
                )
        for name, value in vars(authentication).items():
            if isinstance(value, auth.Way) and name not in auth.WAYS:
                raise TypeError(
                    f"{cls.__name__}.Authentication names a way {name!r}; "
                    f"the names are {', '.join(auth.WAYS)}"
                )
        if authentication.oauth and authentication.sign_in:
            raise TypeError(f"{cls.__name__}.Authentication declares both oauth and sign_in")
        client = authentication.get_client()
        if client and client.identity_endpoint and not client.identity_key:
            raise TypeError(f"{cls.__name__} declares identity_endpoint without identity_key")
        reads_identity = client and (
            client.identity_endpoint
            or authentication.get_identity.__func__ is not auth.Authentication.get_identity.__func__
        )
        if client and client.identity_key and not reads_identity:
            raise TypeError(
                f"{cls.__name__} declares identity_key without identity_endpoint; "
                "declare it or override get_identity"
            )
        authentication.service = cls
        cls.Authentication = authentication
        cls.secret_kind = SecretKind.APP_KEY if authentication.jwt else SecretKind.STATIC
        # The card: ``Settings``, then each way's fields. With two acting ways, each is
        # pasted whole or left out.
        exclusive = len(authentication.get_acting_ways()) > 1
        pieces: dict[str, Any] = {}
        for name, way in authentication.get_ways():
            for field_name, (annotation, info) in way.fields.items():
                if field_name in pieces or field_name in declared.model_fields:
                    raise TypeError(f"{cls.__name__} declares the field {field_name!r} twice")
                if exclusive and name != "sign_in":
                    pieces[field_name] = (
                        annotation | None,
                        FieldInfo.merge_field_infos(info, default=None),
                    )
                else:
                    pieces[field_name] = (annotation, info)
        # A registered service is concrete even under an abstract base.
        cls.abstract = False
        cls.slug = slug
        cls.title = slug.replace("_", " ").title()
        cls.settings_model = create_model(f"{cls.__name__}Settings", __base__=declared, **pieces)
        cls.fields = SimpleNamespace(
            **{name: ServiceField(cls, name) for name in cls.settings_model.model_fields}
        )
        cls.resources = {}
        for value in vars(cls).values():
            if isinstance(value, type) and issubclass(value, Resource):
                value.service = cls
                cls.resources[value.slug] = value
        if cls.is_enabled():
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
        """The service's vault row. ``ServiceDisabledError`` when druks.toml disables the
        service, ``ServiceNotConnectedError`` when nobody connected it."""
        if cls.slug not in services:
            raise ServiceDisabledError(cls.slug)
        if row := await VaultSecret.lookup(
            db_session(), cls.secret_kind, Audience.service(cls.slug)
        ):
            return row
        raise ServiceNotConnectedError(cls.slug)

    @classmethod
    def is_enabled(cls) -> bool:
        """Whether druks.toml leaves this service in the installation. A disabled
        service never registers."""
        return load_settings().services.get(cls.slug, {}).get("enabled", True)

    @classmethod
    def is_configured(cls) -> bool:
        """Whether druks.toml configures this service. Its card then refuses a change."""
        return cls.slug in load_settings().services

    @classmethod
    def is_managed(cls) -> bool:
        """Whether the manager manages this service, as ``manager.services`` says."""
        return cls.slug in load_settings().manager.services

    @classmethod
    async def get_grant(cls) -> VaultSecret | None:
        """The installed app's grant: the OAuth connection the service itself holds."""
        return await VaultSecret.get_service_grant(db_session(), Audience.service(cls.slug))

    @classmethod
    async def get_access_token(cls) -> str:
        """The access token of the installed app's grant."""
        grant = await cls.get_grant()
        if not grant:
            raise ServiceNotConnectedError(cls.slug)
        return await Connection(cls, grant).get_access_token()

    @classmethod
    async def sync_configuration(cls, session: AsyncSession) -> None:
        """Store each service that druks.toml configures as its card."""
        configuration = load_settings()
        undeclared = [slug for slug in configuration.manager.services if not services.get(slug)]
        for slug in undeclared:
            logger.info("No installed app declares the managed service %r. Druks ignores it.", slug)
        for slug, entry in configuration.services.items():
            if slug in undeclared or not entry.get("enabled", True):
                continue
            service = services.get(slug)
            if not service:
                raise ServiceConnectError(
                    f"druks.toml configures the service {slug!r}, and no installed app "
                    f"declares it. Remove [services.{slug}]."
                )
            model = service.settings_model
            if service.is_managed():
                # A key that ``Settings`` does not declare becomes a fact.
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
            row = await VaultSecret.lookup(session, service.secret_kind, Audience.service(slug))
            facts = row.identity if row else {}
            await service._store(
                session,
                settings,
                proven_facts={name: value for name, value in facts.items() if name not in entry},
                kept_secrets={},
            )

    @classmethod
    async def _store(
        cls, session: AsyncSession, settings: BaseModel, proven_facts: dict, kept_secrets: dict
    ) -> VaultSecret:
        secrets = {
            name: getattr(settings, name).get_secret_value()
            for name, field in type(settings).model_fields.items()
            if field_kind(field) == "secret" and getattr(settings, name)
        }
        identity = {
            name: value
            for name, value in settings.model_dump().items()
            if name not in secrets and value
        }
        return await VaultSecret.store(
            session,
            cls.secret_kind,
            Audience.service(cls.slug),
            identity={**identity, **proven_facts},
            secrets={**kept_secrets, **secrets},
        )

    @classmethod
    def with_scopes(cls, *scopes: str) -> ScopedService:
        """Declare this app's use of the service and the scopes its
        calls need."""
        if cls.Authentication.has_sign_in():
            return ScopedService(cls, scopes)
        raise TypeError(f"{cls.__name__} offers no sign-in")

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
        if client := cls.Authentication.get_client():
            scopes.update(client.identity_scopes)
        return tuple(sorted(scopes))

    @classmethod
    def get_profile(cls, identity: dict[str, Any]) -> dict[str, Any]:
        """The facts that name the person at the provider: login, email, name. Not the
        authority or the identity key, which only match the sign-in to its account."""
        client = cls.Authentication.get_client()
        hidden = ("authority", client.identity_key if client else "")
        return {key: value for key, value in identity.items() if key not in hidden}

    @classmethod
    async def get_oauth_client(cls) -> OauthClient:
        client = cls.Authentication.get_client()
        if not client:
            raise TypeError(f"{cls.__name__} declares no OAuth client")
        connected = await cls.get()
        if cls.is_managed():
            manager = load_settings().manager
            return OauthClient(
                provider=cls.slug,
                authorization_endpoint=client.authorization_endpoint,
                token_endpoint=f"{connected.identity['url'].rstrip('/')}/oauth/token",
                client_id=connected.identity["client_id"],
                extra_authorize_params=client.extra_authorize_params,
                token_headers={"Authorization": f"Bearer {manager.token.get_secret_value()}"},
                state_prefix=f"{manager.audience}.",
                redirect_uri=f"{manager.issuer.rstrip('/')}/{cls.slug}/oauth/callback",
                is_grant_revoked=cls.Authentication.is_grant_revoked,
            )
        return OauthClient(
            provider=cls.slug,
            authorization_endpoint=client.authorization_endpoint,
            token_endpoint=client.token_endpoint,
            client_id=connected.identity["client_id"],
            client_secret=connected.secrets["client_secret"],
            basic_auth=client.basic_auth,
            extra_authorize_params=client.extra_authorize_params,
            is_grant_revoked=cls.Authentication.is_grant_revoked,
        )

    @classmethod
    async def install(cls, session: AsyncSession, tokens: dict[str, Any]) -> None:
        """Record what the install proves. With an ``oauth`` way, keep its grant as the
        service's own in place of the previous one, and drop a pasted key: a service holds
        one way at a time."""
        await cls.Authentication.complete_install(tokens)
        if cls.Authentication.oauth:
            audience = Audience.service(cls.slug)
            if previous := await VaultSecret.get_service_grant(session, audience):
                await OauthClient(provider=cls.slug).disconnect(previous, reason="reinstalled")
            refresh_token = tokens.get("refresh_token", "")
            await VaultSecret.connect(
                session,
                audience,
                account_id=None,
                refresh_token=refresh_token,
                scopes=tokens.get("scope", "").replace(",", " ").split(),
                secrets={} if refresh_token else {"access_token": tokens["access_token"]},
            )
            row = await cls.get()
            keys = {
                field_name
                for name, way in cls.Authentication.get_acting_ways()
                if name != "oauth"
                for field_name in way.fields
            }
            row.secrets = {name: value for name, value in row.secrets.items() if name not in keys}

    @classmethod
    async def issue_token(
        cls, resource: str = "", *, name: str = ""
    ) -> tuple[str, datetime | None]:
        """The token a sandbox fetches under ``name``, and its expiry: a resource recipe's
        when ``resource`` names one, else what the service's ``Authentication`` gives."""
        if resource:
            slug, _, key = resource.partition("/")
            if slug in cls.resources:
                return await cls.resources[slug](key).get_token()
        return await cls.Authentication.issue_token(resource, name=name)

    @classmethod
    async def is_connected(cls) -> bool:
        """Whether the acting way that is present has what it needs: a pasted key's secrets
        on the row, or the installed app's grant. A service that only signs people in is
        connected by its row."""
        row = await VaultSecret.lookup(db_session(), cls.secret_kind, Audience.service(cls.slug))
        if not row:
            return False
        acting = cls.Authentication.get_acting_ways()
        if not acting:
            return True
        for name, way in acting:
            if name == "oauth":
                if await cls.get_grant():
                    return True
            elif all(
                field_name in row.secrets
                for field_name in way.fields
                if field_kind(cls.settings_model.model_fields[field_name]) == "secret"
            ):
                return True
        return False

    @classmethod
    async def has_login(cls) -> bool:
        """Whether the service holds a login of its own: a pasted key, or the installed app's
        grant. An App's private key is no login."""
        return (
            bool(cls.Authentication.api_key or cls.Authentication.oauth)
            and await cls.is_connected()
        )

    @classmethod
    async def connect(cls, payload: dict[str, str]) -> VaultSecret:
        """Verify and store a paste of the service's fields: secrets encrypted, the rest as
        identity facts. On a connected card a blank secret keeps the stored one. A pasted key
        ends the installed app's grant: a service holds one way at a time. Another client ID
        revokes the connections that belong to the old one."""
        if cls.is_configured():
            raise ServiceManagedError(cls.slug)
        fields = cls.settings_model.model_fields
        session = db_session()
        previous = await VaultSecret.lookup(session, cls.secret_kind, Audience.service(cls.slug))
        previous_client_id = previous.identity.get("client_id") if previous else None
        pasted = dict(previous.secrets) if previous else {}
        for name, value in payload.items():
            if name in fields:
                if field_kind(fields[name]) != "secret":
                    value = value.strip()
                if value:
                    pasted[name] = value
        labels = {spec["name"]: spec["label"] for spec in cls.connect_fields()}
        # A way is pasted whole or left out.
        for _, way in cls.Authentication.get_ways():
            names = list(way.fields)
            missing = [name for name in names if name not in pasted]
            if missing and len(missing) < len(names):
                raise ServiceConnectError(f"Enter {', '.join(labels[name] for name in missing)}.")
        try:
            settings = cls.settings_model.model_validate(pasted)
        except ValidationError as error:
            missing = ", ".join(labels[item["loc"][0]] for item in error.errors())
            raise ServiceConnectError(f"Enter {missing}.") from error
        row = await cls._store(
            session, settings, proven_facts=await cls.verify(settings), kept_secrets={}
        )
        client = OauthClient(provider=cls.slug)
        if cls.Authentication.get_client() and row.identity.get("client_id") != previous_client_id:
            for connection in await VaultSecret.list_connections(
                session, Audience.service(cls.slug)
            ):
                await client.disconnect(connection, reason="client_replaced")
        pasted_key = any(
            payload.get(field_name)
            for name, way in cls.Authentication.get_acting_ways()
            if name != "oauth"
            for field_name in way.fields
        )
        if pasted_key and (
            grant := await VaultSecret.get_service_grant(session, Audience.service(cls.slug))
        ):
            await client.disconnect(grant, reason="key_pasted")
        return row

    @classmethod
    async def disconnect(cls, session: AsyncSession) -> None:
        """Revoke the card, every account's sign-in, and the login its MCP servers derived.
        The provider keeps its application."""
        audience = Audience.service(cls.slug)
        client = OauthClient(provider=cls.slug)
        for connection in await VaultSecret.list_connections(session, audience):
            await client.disconnect(connection, reason="service_disconnected")
        if row := await VaultSecret.lookup(session, cls.secret_kind, audience):
            await row.revoke("user")
        await McpServer.remove_login(session, cls.mcp_host)

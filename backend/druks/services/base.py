from typing import Any, ClassVar

from pydantic import BaseModel, ValidationError

from druks.extensions.base import NAME_RE
from druks.extensions.loader import iter_extensions
from druks.extensions.registry import services
from druks.extensions.settings import field_kind, field_multiline

from .exceptions import ServiceConnectError, ServiceNotConnectedError
from .models import OauthConnection, ServiceIdentity
from .oauth import OauthClient


class Connection:
    """One signed-in provider account, reached through the extension's
    declared handle. Mint and disconnect act on this sign-in only."""

    def __init__(self, service: "type[Service]", row: OauthConnection) -> None:
        self.service = service
        self.row = row

    @property
    def id(self) -> str:
        return self.row.id

    @property
    def scopes(self) -> list[str]:
        return self.row.scopes

    @property
    def connected_at(self):
        return self.row.connected_at

    async def mint_access_token(self) -> str:
        return await self.service.get_oauth_client().mint_access_token(connection=self.row)

    async def disconnect(self) -> None:
        await OauthClient(provider=self.service.name).disconnect(self.row)


class ScopedService:
    """A service seen through one extension's declared scopes
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

    def list_for_account(self, account_id: str) -> list[Connection]:
        return [
            Connection(self.service, row)
            for row in OauthConnection.list_for_account(self.service.name, account_id)
        ]

    def get(self, connection_id: str) -> Connection | None:
        row = OauthConnection.get(connection_id)
        if row and row.provider == self.service.name:
            return Connection(self.service, row)


class Service:
    """The appliance's own identity at an external provider — one per service,
    declared by the code that consumes it. Subclass in a ``services`` module,
    set ``name`` and an inner ``Settings`` model; the platform renders the
    connect card, verifies and stores the paste, and reports doctor state, all
    from the declaration. Read back through the same class:
    ``Gmail.get().secrets["client_secret"]``.

    Used as a class, never instantiated — the same install-singleton shape as
    ``Extension``.
    """

    name: ClassVar[str]
    title: ClassVar[str]
    description: ClassVar[str] = ""
    # Whether doctor fails when this service is not connected.
    required: ClassVar[bool] = True
    settings_model: ClassVar[type[BaseModel]]
    # Set both endpoints when the registered app is an OAuth client;
    # ``get_oauth_client()`` then hands back the connected identity as a
    # configured ``OauthClient``. Scopes are not declared here — the
    # extensions that use the service declare them (``connection``), and
    # the connect door asks for their union.
    authorization_endpoint: ClassVar[str] = ""
    token_endpoint: ClassVar[str] = ""
    # HTTP Basic on the token endpoint; False sends the secret in the body.
    basic_auth: ClassVar[bool] = False

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        name = getattr(cls, "name", None)
        if not name:
            raise TypeError(f"{cls.__name__} must set a `name`")
        if not getattr(cls, "title", None):
            raise TypeError(f"{cls.__name__} must set a `title` — the connect card's heading")
        if not NAME_RE.match(name):
            raise TypeError(
                f"service name {name!r} must match {NAME_RE.pattern!r} — it keys the "
                "service_identities row and the connect wire"
            )
        declared = cls.__dict__.get("Settings")
        if not isinstance(declared, type) or not issubclass(declared, BaseModel):
            raise TypeError(f"{cls.__name__}.Settings must be a pydantic model")
        if bool(cls.authorization_endpoint) != bool(cls.token_endpoint):
            raise TypeError(f"{cls.__name__} must declare both OAuth endpoints or neither")
        if cls.token_endpoint and not {"client_id", "client_secret"} <= set(declared.model_fields):
            raise TypeError(
                f"{cls.__name__}.Settings must declare client_id and client_secret "
                "fields — get_oauth_client() reads the OAuth client from them"
            )
        cls.settings_model = declared
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
            }
            for name, field in cls.settings_model.model_fields.items()
        ]

    @classmethod
    def get(cls) -> ServiceIdentity:
        return ServiceIdentity.get(cls.name)

    @classmethod
    def with_scopes(cls, *scopes: str) -> ScopedService:
        """Declare this extension's use of the service and the scopes its
        calls need."""
        if not cls.token_endpoint:
            raise TypeError(f"{cls.__name__} declares no OAuth endpoints")
        return ScopedService(cls, scopes)

    @classmethod
    def declarations(cls) -> "list[ScopedService]":
        return [
            value
            for extension in iter_extensions()
            for value in vars(extension).values()
            if isinstance(value, ScopedService) and value.service is cls
        ]

    @classmethod
    def required_scopes(cls) -> tuple[str, ...]:
        """The union of every installed declaration's scopes — the consent ask."""
        scopes = {scope for declaration in cls.declarations() for scope in declaration.scopes}
        return tuple(sorted(scopes))

    @classmethod
    def get_oauth_client(cls) -> OauthClient:
        """The connected identity as a configured ``OauthClient``, keyed by
        the service name. Raises ``ServiceNotConnectedError`` until the
        operator connects the service."""
        if not cls.token_endpoint:
            raise TypeError(f"{cls.__name__} declares no OAuth endpoints")
        connected = cls.get()
        return OauthClient(
            provider=cls.name,
            authorization_endpoint=cls.authorization_endpoint,
            token_endpoint=cls.token_endpoint,
            client_id=connected.identity["client_id"],
            client_secret=connected.secrets["client_secret"],
            basic_auth=cls.basic_auth,
        )

    @classmethod
    def is_connected(cls) -> bool:
        try:
            ServiceIdentity.get(cls.name)
        except ServiceNotConnectedError:
            return False
        return True

    @classmethod
    async def connect(cls, payload: dict[str, Any]) -> ServiceIdentity:
        """Verify and store a full paste of the service's fields. Secret fields
        land in the encrypted ``secrets``, the rest become ``identity`` facts.
        Plain fields are stripped as pasted; secrets are stored byte-for-byte."""
        fields = cls.settings_model.model_fields
        pasted: dict[str, Any] = {}
        for name, value in payload.items():
            if name not in fields:
                continue
            if isinstance(value, str) and field_kind(fields[name]) != "secret":
                value = value.strip()
            pasted[name] = value
        try:
            settings = cls.settings_model.model_validate(pasted)
        except ValidationError as error:
            raise ServiceConnectError("Every field is required.") from error
        secrets = {
            name: getattr(settings, name).get_secret_value()
            for name, field in fields.items()
            if field_kind(field) == "secret"
        }
        identity = {
            name: getattr(settings, name)
            for name, field in fields.items()
            if field_kind(field) != "secret"
        }
        if all(str(value).strip() for value in (*identity.values(), *secrets.values())):
            proven = await cls.verify(settings)
            return ServiceIdentity.connect(
                cls.name, identity={**identity, **proven}, secrets=secrets
            )
        raise ServiceConnectError("Every field is required.")

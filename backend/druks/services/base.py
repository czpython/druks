from typing import Any, ClassVar

from pydantic import BaseModel, ValidationError

from druks.extensions.base import NAME_RE
from druks.extensions.registry import services
from druks.extensions.settings import field_kind, field_multiline

from .exceptions import ServiceConnectError, ServiceNotConnectedError
from .models import ServiceIdentity


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

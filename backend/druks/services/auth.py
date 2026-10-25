"""How a service authenticates. A service declares one nested class named ``Authentication``,
found by name like ``Settings``. It lists the ways Druks acts as the service, held one at a
time, and the client people sign in through, as instances configured by keyword, and it
carries the provider's authentication behavior as classmethods."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, ClassVar

import httpx
from pydantic import Field, SecretStr
from pydantic.fields import FieldInfo

from druks.secrets.exceptions import MissingSecretError
from druks.secrets.models import VaultSecret

from . import oauth
from .exceptions import OauthExchangeError

if TYPE_CHECKING:
    from .base import Service


@dataclass(frozen=True, kw_only=True)
class Way:
    """A way a service authenticates. ``fields`` are what the operator pastes for it."""

    @property
    def fields(self) -> dict[str, tuple[Any, FieldInfo]]:
        return {}


@dataclass(frozen=True, kw_only=True)
class ApiKey(Way):
    """A pasted key, sent the way the provider takes it."""

    @property
    def fields(self) -> dict[str, tuple[Any, FieldInfo]]:
        return {"api_key": (SecretStr, Field(title="API key"))}


@dataclass(frozen=True, kw_only=True)
class Jwt(Way):
    """An app's private key. The service signs a JWT with it and mints its own tokens."""

    @property
    def fields(self) -> dict[str, tuple[Any, FieldInfo]]:
        return {
            "private_key": (
                SecretStr,
                Field(title="Private key (PEM)", json_schema_extra={"multiline": True}),
            )
        }


@dataclass(frozen=True, kw_only=True)
class Client(Way):
    """An OAuth client at the provider, with the endpoints people sign in through."""

    authorization_endpoint: str
    token_endpoint: str
    # HTTP Basic on the token endpoint; False sends the secret in the body.
    basic_auth: bool = False
    # The provider's consent-query quirks — Google grants a refresh token
    # only with access_type=offline and prompt=consent.
    extra_authorize_params: dict[str, str] = field(default_factory=dict)
    # The endpoint that returns the signed-in account's facts;
    # identity_scopes join the consent ask.
    identity_endpoint: str = ""
    identity_scopes: tuple[str, ...] = ()
    # The identity fact that names the provider account — "sub" for Google,
    # "subject" for GitHub. When set, people sign in through the client, and a fresh
    # sign-in that matches an existing connection for the same owner updates that row.
    identity_key: str = ""

    @property
    def fields(self) -> dict[str, tuple[Any, FieldInfo]]:
        return {
            "client_id": (str, Field(title="Client ID")),
            "client_secret": (SecretStr, Field(title="Client secret")),
        }


@dataclass(frozen=True, kw_only=True)
class Oauth(Client):
    """The service's OAuth app. Druks acts with the grant its install returns."""

    # Query parameters of the install request, such as Slack's bot scope.
    install_params: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, kw_only=True)
class SignIn(Client):
    """An OAuth client people only sign in through. Druks never acts with it."""


# The names a service gives its ways, and the kind each holds.
WAYS: dict[str, type[Way]] = {"api_key": ApiKey, "jwt": Jwt, "oauth": Oauth, "sign_in": SignIn}


class Authentication:
    """The defaults of a service's authentication. A service's own ``Authentication`` names
    its ways and overrides what its provider does differently."""

    service: ClassVar["type[Service]"]
    api_key: ClassVar[ApiKey | None] = None
    jwt: ClassVar[Jwt | None] = None
    oauth: ClassVar[Oauth | None] = None
    sign_in: ClassVar[SignIn | None] = None

    @classmethod
    def get_acting_ways(cls) -> list[tuple[str, Way]]:
        """The ways Druks acts as the service, in the card's order."""
        ways = (("api_key", cls.api_key), ("jwt", cls.jwt), ("oauth", cls.oauth))
        return [(name, way) for name, way in ways if way]

    @classmethod
    def get_ways(cls) -> list[tuple[str, Way]]:
        """Every way, the acting ones first, then the sign-in client."""
        return cls.get_acting_ways() + ([("sign_in", cls.sign_in)] if cls.sign_in else [])

    @classmethod
    def get_client(cls) -> Client | None:
        """The one OAuth client of the service: its app, or the client people sign in through."""
        return cls.oauth or cls.sign_in

    @classmethod
    def has_sign_in(cls) -> bool:
        """Whether people sign in through the service's client: it names the identity fact that
        tells them apart."""
        client = cls.get_client()
        return bool(client and client.identity_key)

    @classmethod
    def get_authorization(cls, login: VaultSecret) -> str:
        """The ``Authorization`` value of the connected row, which the default account
        sends to the server at ``mcp_host``: the API key as a bearer token."""
        return f"Bearer {login.secrets['api_key']}"

    @classmethod
    async def get_identity(cls, access_token: str) -> dict[str, Any]:
        """The signed-in account's facts, read from the client's ``identity_endpoint``.
        Override when the provider needs a different call."""
        client = cls.get_client()
        if client and client.identity_endpoint:
            return await oauth.fetch_identity(client.identity_endpoint, access_token)
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
                cls.service.slug,
                "the authorization server granted no refresh token; druks needs offline access",
                context={},
            )
        return {
            "access_token": tokens["access_token"],
            "refresh_token": tokens["refresh_token"],
            "scopes": tokens.get("scope", "").split(),
        }

    @classmethod
    def is_grant_revoked(cls, response: httpx.Response) -> bool:
        """Whether the token endpoint's answer to a refresh says the provider revoked
        the grant. Override for a provider that reports it otherwise than RFC 6749."""
        return oauth.is_grant_revoked(response)

    @classmethod
    async def get_install_endpoint(cls) -> str:
        """Where the install of the service's app starts: its authorize endpoint. Override for
        a provider with an install page of its own."""
        return cls.oauth.authorization_endpoint if cls.oauth else ""

    @classmethod
    async def complete_install(cls, tokens: dict[str, Any]) -> None:
        """Record what the install proves. The default records nothing."""

    @classmethod
    async def issue_token(
        cls, resources: tuple[str, ...] = (), *, name: str = ""
    ) -> tuple[str, datetime | None]:
        """The token a sandbox fetches under ``name``, and its expiry. ``name`` is
        ``<slug>_<field>`` for a pasted field; under any other name the installed app's
        access token, else the one secret the row holds. Override for a provider that
        mints a token per resource."""
        row = await cls.service.get()
        field_name = name.removeprefix(f"{cls.service.slug}_")
        if field_name in row.secrets:
            return row.secrets[field_name], None
        if cls.oauth and (grant := await cls.service.get_grant()):
            client = await cls.service.get_oauth_client()
            return await client.get_access_token(grant.session, connection=grant)
        if len(row.secrets) != 1:
            raise MissingSecretError(
                f"{cls.service.slug} holds {len(row.secrets)} secrets. Name the field."
            )
        [secret] = row.secrets.values()
        return secret, None

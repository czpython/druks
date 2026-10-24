from typing import TYPE_CHECKING, ClassVar

from druks.secrets.enums import SecretKind

if TYPE_CHECKING:
    from .base import Service


class BaseIntegration:
    """The service's app at the provider: the OAuth endpoints people sign in through, when it
    has them, and the install. ``Settings`` holds the client's ``client_id`` and
    ``client_secret``."""

    service: ClassVar["type[Service]"]
    # What the connected row is in the vault.
    secret_kind: ClassVar[SecretKind] = SecretKind.STATIC
    # Whether the install grants the service a token of its own, kept as its grant.
    stores_grant: ClassVar[bool] = False
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
    # Query parameters of the install request, such as Slack's bot scope.
    install_params: ClassVar[dict[str, str]] = {}

    @classmethod
    async def get_install_endpoint(cls) -> str:
        """Where the install starts: the authorize endpoint, unless the provider has an
        install page."""
        return cls.authorization_endpoint


class Oauth(BaseIntegration):
    """An OAuth app. Its install grants the service its own token."""

    stores_grant = True


class PrivateKey(BaseIntegration):
    """An app with a private key, as a GitHub App. It mints its own tokens per resource; its
    install binds installations and grants no token."""

    secret_kind = SecretKind.APP_KEY

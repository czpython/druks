from dataclasses import dataclass
from typing import Any

from druks.mcp.enums import Credential
from druks.secrets.models import VaultSecret


@dataclass(frozen=True)
class McpServerAccess:
    """One MCP server as one account reaches it."""

    name: str
    url: str
    is_oauth: bool
    is_enabled: bool
    # A catalog definition, which Druks manages: disable it, never remove it.
    builtin: bool
    identity_mode: str | None
    # The scopes a connect requests. None requests what the server advertises.
    scopes: list[str] | None
    headers: dict[str, Any]
    secret_headers: dict[str, VaultSecret]
    # The service that owns the server's host.
    service: str | None
    credential: Credential
    # The account's grant, its sign-in at the service, or the service's login.
    # None for a header server, and for an account that has not connected.
    secret: VaultSecret | None

    @property
    def has_token(self) -> bool:
        """Whether nothing blocks the server's auth at delivery."""
        if self.credential == Credential.HEADERS:
            return bool(self.secret_headers)
        return bool(self.secret)

    @property
    def supports_scope_selection(self) -> bool:
        """Whether Druks runs the server's OAuth flow itself, and so can choose its scopes."""
        return self.is_oauth and self.credential != Credential.SERVICE_CONNECTION

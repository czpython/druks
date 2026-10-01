from enum import Enum, StrEnum
from typing import Literal


class IdentityMode(StrEnum):
    SHARED = "shared"
    PER_USER = "per_user"


class Credential(StrEnum):
    """Where an account's credential for an MCP server comes from."""

    # The server's secret header rows: the installation's, or the account's own.
    HEADERS = "headers"
    # The account's own OAuth grant at the server.
    GRANT = "grant"
    # The account's sign-in at the service that owns the server's host.
    SERVICE_CONNECTION = "service_connection"
    # The pasted login of that service, which only the default account sends.
    SERVICE_LOGIN = "service_login"


class Toolkit(Enum):
    """What a Druks key allows when it has no tool list: the whole API."""

    ALL = "all"


# The tools a Druks key allows. An empty tuple allows no tool.
AllowedTools = tuple[str, ...] | Literal[Toolkit.ALL]

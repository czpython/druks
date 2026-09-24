from sqlalchemy.ext.asyncio import AsyncSession

from druks.accounts.models import PersonalAccessToken
from druks.mcp.constants import BEARER_HEADER, BEARER_PREFIX, DRUKS_SERVER_NAME
from druks.mcp.enums import AllowedTools, Toolkit
from druks.mcp.exceptions import MissingEndpointError
from druks.sandbox.datastructures import RequiredMcpServer
from druks.secrets.datastructures import Audience
from druks.secrets.enums import SecretKind
from druks.secrets.models import VaultSecret
from druks.settings import load_settings


def get_druks_mcp_server(*, allowed_tools: tuple[str, ...]) -> RequiredMcpServer:
    """Druks' own `/mcp` as a workspace requires it, at the address a box reaches."""
    if endpoint := load_settings().urls.webhook_base:
        return RequiredMcpServer(
            name=DRUKS_SERVER_NAME, url=f"{endpoint}/mcp", allowed_tools=allowed_tools
        )
    raise MissingEndpointError(DRUKS_SERVER_NAME)


async def get_druks_account_token(
    session: AsyncSession,
    account_id: str,
    allowed_tools: AllowedTools,
    *,
    name: str = DRUKS_SERVER_NAME,
) -> VaultSecret:
    """Get this account's key with this name, or mint it when the key is missing, no
    longer active, or allows other tools. A replaced key is revoked."""
    audience = Audience.mcp(name)
    # A key row with no tool list allows the whole API.
    allowed = None if allowed_tools is Toolkit.ALL else list(allowed_tools)
    row = await VaultSecret.lookup(session, SecretKind.STATIC, audience, account_id, BEARER_HEADER)
    if row:
        held_token = await PersonalAccessToken.get_for_prefix(session, row.identity["token_prefix"])
        if held_token and held_token.status == "active" and held_token.allowed_tools == allowed:
            return row
        if held_token:
            await held_token.revoke()
    minted, token = await PersonalAccessToken.create(
        session,
        account_id=account_id,
        name=f"{name} MCP",
        allowed_tools=allowed,
    )
    return await VaultSecret.store(
        session,
        SecretKind.STATIC,
        audience,
        # A header row holds the verbatim header value; the box adds no prefix.
        secrets={"value": f"{BEARER_PREFIX}{token}"},
        identity={"token_prefix": minted.token_prefix},
        account_id=account_id,
        header=BEARER_HEADER,
    )

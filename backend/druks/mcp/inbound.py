from sqlalchemy.ext.asyncio import AsyncSession

from druks.accounts.models import PersonalAccessToken
from druks.mcp.constants import BEARER_HEADER, DRUKS_SERVER_NAME
from druks.mcp.exceptions import MissingEndpointError
from druks.sandbox.datastructures import RequiredMcpServer
from druks.secrets.datastructures import Audience
from druks.secrets.enums import SecretKind
from druks.secrets.models import VaultSecret
from druks.settings import load_settings


def get_druks_mcp_server(*, allowed_tools: tuple[str, ...]) -> RequiredMcpServer:
    """Druks' own `/mcp` as a workspace requires it, at the address a box reaches."""
    urls = load_settings().urls
    endpoint = f"https://{urls.webhook_host}" if urls.webhook_host else urls.endpoint
    if endpoint := endpoint.rstrip("/"):
        return RequiredMcpServer(
            name=DRUKS_SERVER_NAME, url=f"{endpoint}/mcp", allowed_tools=allowed_tools
        )
    raise MissingEndpointError(DRUKS_SERVER_NAME)


async def get_druks_account_token(
    session: AsyncSession,
    account_id: str,
    allowed_tools: tuple[str, ...],
    *,
    name: str = DRUKS_SERVER_NAME,
) -> VaultSecret:
    """Get this account's key with this name, or mint it on first use."""
    audience = Audience.mcp(name)
    row = await VaultSecret.lookup(session, SecretKind.STATIC, audience, account_id, BEARER_HEADER)
    if row:
        held = await PersonalAccessToken.get_for_prefix(session, row.identity["token_prefix"])
        if held and held.status == "active":
            return row
    minted, token = await PersonalAccessToken.create(
        session,
        account_id=account_id,
        name=f"{name} MCP",
        allowed_tools=list(allowed_tools) or None,
    )
    return await VaultSecret.store(
        session,
        SecretKind.STATIC,
        audience,
        secrets={"value": token},
        identity={"token_prefix": minted.token_prefix},
        account_id=account_id,
        header=BEARER_HEADER,
    )

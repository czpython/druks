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
    if endpoint := load_settings().urls.endpoint.rstrip("/"):
        return RequiredMcpServer(
            name=DRUKS_SERVER_NAME, url=f"{endpoint}/mcp", allowed_tools=allowed_tools
        )
    raise MissingEndpointError(DRUKS_SERVER_NAME)


async def get_druks_account_token(account_id: str, allowed_tools: tuple[str, ...]) -> VaultSecret:
    """This account's token row, minted when a run of theirs first needs it."""
    audience = Audience.mcp(DRUKS_SERVER_NAME)
    row = await VaultSecret.lookup(SecretKind.STATIC, audience, account_id, BEARER_HEADER)
    held = await PersonalAccessToken.get_for_prefix(row.identity["token_prefix"]) if row else None
    if held and held.status == "active":
        return row
    minted, token = await PersonalAccessToken.create(
        account_id=account_id,
        name=f"{DRUKS_SERVER_NAME} MCP",
        allowed_tools=list(allowed_tools) or None,
    )
    return await VaultSecret.store(
        SecretKind.STATIC,
        audience,
        secrets={"value": token},
        identity={"token_prefix": minted.token_prefix},
        account_id=account_id,
        header=BEARER_HEADER,
    )

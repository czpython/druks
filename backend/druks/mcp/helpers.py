from druks.accounts.constants import SYSTEM_ACCOUNT_ID
from druks.mcp.constants import TOKEN_ENV_PREFIX, TOKEN_ENV_SUFFIX
from druks.mcp.enums import IdentityMode
from druks.mcp.exceptions import UnresolvedGrantAccountError


def get_bearer_token_env_var(name: str) -> str:
    return f"{TOKEN_ENV_PREFIX}{name.upper()}{TOKEN_ENV_SUFFIX}"


def grant_provider(name: str) -> str:
    # Namespaced so a server name can never collide with a Service name in
    # the shared grant table and Redis keys.
    return f"mcp:{name}"


def get_grant_account(identity_mode: str | None, run_account_id: str | None) -> str:
    # Whose grant serves this caller: a shared server's grant lives under
    # the system account whoever asks; a per-user server's under the asker.
    if identity_mode == IdentityMode.PER_USER and run_account_id:
        return run_account_id
    if identity_mode == IdentityMode.PER_USER:
        raise UnresolvedGrantAccountError(identity_mode, run_account_id)
    if identity_mode == IdentityMode.SHARED:
        return SYSTEM_ACCOUNT_ID
    raise UnresolvedGrantAccountError(identity_mode, run_account_id)

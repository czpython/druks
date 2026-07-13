from druks.mcp.constants import TOKEN_ENV_PREFIX, TOKEN_ENV_SUFFIX


def get_bearer_token_env_var(name: str) -> str:
    return f"{TOKEN_ENV_PREFIX}{name.upper()}{TOKEN_ENV_SUFFIX}"

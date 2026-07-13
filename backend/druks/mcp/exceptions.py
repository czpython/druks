class McpServerError(Exception):
    pass


class InvalidServerNameError(McpServerError):
    # The name is one identifier reused as the MCP config key and the bearer
    # env-var stem, so it must be a lowercase shell/TOML-safe token — reject
    # anything else at creation rather than emit a broken config in the VM.
    def __init__(self, name: str):
        super().__init__(
            f"Invalid MCP server name {name!r}: use lowercase letters, digits and "
            "underscores, starting with a letter (e.g. 'linear', 'linear_app')."
        )
        self.name = name


class MissingTokenError(McpServerError):
    # An enabled server carries no token, so it can't authenticate in the VM.
    # Raised loudly at delivery rather than shipping a header the harness can't
    # fill — the silent-degrade path this subsystem exists to close.
    def __init__(self, name: str):
        super().__init__(f"Enabled MCP server {name!r} has no token; it cannot authenticate.")
        self.name = name


class SourceEnvVarUnsetError(McpServerError):
    # An env-sourced server reads its token from druks' own process env at
    # delivery; an unset var means it can't authenticate. Raised loudly, naming
    # the var the operator must set, rather than shipping a dead server.
    def __init__(self, name: str, source_env_var: str):
        super().__init__(
            f"Enabled MCP server {name!r} reads its token from ${source_env_var}, "
            "which is not set in druks' environment."
        )
        self.name = name
        self.source_env_var = source_env_var


class InvalidCatalogError(McpServerError):
    # The catalog declares a deployment's default servers; a file that can't be
    # read or an entry that would emit a broken config stops boot by name —
    # never a silent drop of servers from every agent VM.
    def __init__(self, path, reason: str):
        super().__init__(f"Invalid MCP catalog {path}: {reason}")
        self.path = path
        self.reason = reason


class RegistryUnavailableError(McpServerError):
    # The registry couldn't answer — network trouble, a non-2xx, or an
    # undocumented payload shape. Loud, so an unreachable registry never
    # reads as "no such server".
    def __init__(self, query: str, reason: str):
        super().__init__(f"MCP registry search for {query!r} failed: {reason}")
        self.query = query
        self.reason = reason


class OauthConnectError(McpServerError):
    # The connect flow (discovery, client registration, code exchange) failed —
    # surfaced to the operator who clicked Connect, with the step that broke.
    # Nothing is stored on failure, so re-connecting is always safe.
    def __init__(self, name: str, reason: str):
        super().__init__(f"OAuth connect for MCP server {name!r} failed: {reason}")
        self.name = name
        self.reason = reason


class MissingGrantError(McpServerError):
    # An enabled OAuth server has no stored grant, so delivery can't mint a
    # token for it. Raised loudly at delivery — the operator must run the
    # connect flow (or disable the server), not discover a dead server mid-run.
    def __init__(self, name: str):
        super().__init__(
            f"Enabled MCP server {name!r} is not connected; complete its OAuth "
            "connect flow or disable it."
        )
        self.name = name


class GrantRefreshError(McpServerError):
    # Minting an access token from the stored grant failed (the authorization
    # server rejected the refresh token, or the token endpoint is unreachable).
    # Raised loudly at delivery, never mid-run; re-connecting replaces the grant.
    def __init__(self, name: str, reason: str):
        super().__init__(
            f"Could not mint an access token for MCP server {name!r}: {reason}. "
            "Re-connect the server if its grant was revoked."
        )
        self.name = name
        self.reason = reason

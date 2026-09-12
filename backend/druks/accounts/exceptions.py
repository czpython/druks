from druks.api.exceptions import AgentApiError


class CredentialReadOnly(AgentApiError):
    # A credential whose writes are denied, presented on a write request. The
    # door refuses it, so the mode holds on every route, not only MCP tools.
    status_code = 403
    code = "CREDENTIAL_READ_ONLY"

    def __init__(self) -> None:
        super().__init__("This credential may only read.")


class InvalidPatError(Exception):
    """A presented bearer credential that resolves to no live personal access
    token — unknown, mismatched, revoked, or expired."""


class AuthConfigurationError(Exception):
    """The configured auth mode cannot resolve a single operator identity —
    e.g. ``none`` mode with more than one account. Refuses the
    request (and startup) instead of guessing which account is the operator."""


class InvalidAssertionError(Exception):
    """An edge-minted JWT assertion that fails verification — bad signature,
    wrong issuer or audience, expired, unknown signing key, or a missing
    identity claim. The raw token never appears in the message."""

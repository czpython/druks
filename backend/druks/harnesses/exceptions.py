class HarnessError(Exception):
    pass


class StreamJsonError(ValueError):
    """Claude's ``--output-format stream-json`` produced no usable events."""


class HarnessTimeoutError(HarnessError):
    pass


class OAuthTokenError(Exception):
    """No usable subscription credential is available.

    ``tag`` is a short, stable code surfaced on the usage snapshot's
    ``error`` column: ``no_credentials`` (harness not connected),
    ``no_token`` (credential present, no access token), ``token_expired``
    (past expiry; the refresh cron hasn't caught up).
    """

    def __init__(self, tag: str, message: str | None = None) -> None:
        super().__init__(message or tag)
        self.tag = tag


class GrantError(Exception):
    """A token-refresh grant produced no usable grant. ``tag`` is the short,
    stable code recorded on the rotation report: ``network`` (request never
    completed), ``invalid_grant`` (provider revoked/rejected the refresh
    token — reconnect to fix), ``bad_response`` (200 with an unusable body),
    or ``http_<status>``."""

    def __init__(self, tag: str) -> None:
        super().__init__(tag)
        self.tag = tag


class ConnectError(Exception):
    """A connect flow could not complete — expired/single-use pending state, a
    paste with no code, a state mismatch, or a provider-rejected exchange. The
    message is user-facing (surfaced inline in the Settings card)."""


class HarnessNotConnectedError(HarnessError):
    """The harness has no stored subscription credential, so nothing that needs
    auth can run. Connecting in Settings → Harnesses is the only credential
    path — there is no host-file or baked-API-key fallback — which is what
    makes "is this harness runnable" decidable before any VM work."""


class HarnessFirstByteTimeoutError(HarnessError):
    """A harness subprocess produced zero stdout bytes within the
    first-byte deadline and was killed.

    Distinct from :class:`HarnessTimeoutError` (which represents the
    full per-operation budget elapsing) so callers can decide whether
    to retry vs. escalate. A first-byte miss almost always indicates
    a pre-LLM wedge in the CLI (event-loop hang, MCP load failure,
    upstream HTTP stall the CLI didn't surface) rather than slow
    legitimate inference, so retries are usually safe.
    """

from enum import StrEnum
from typing import ClassVar


class Retry(StrEnum):
    # Spaced attempts now, once the quota window resets, or not at all.
    TRANSIENT = "transient"
    QUOTA = "quota"
    NEVER = "never"


class HarnessError(Exception):
    # Recorded beside the message on the failed call and its run; "" means
    # unclassified, which is never retried.
    code: ClassVar[str] = ""
    retry: ClassVar[Retry] = Retry.NEVER
    retry_delays: ClassVar[tuple[int, ...]] = ()


class StreamJsonError(ValueError):
    """Claude's ``--output-format stream-json`` produced no usable events."""


class HarnessTimeoutError(HarnessError):
    """The full per-operation budget elapsed. Never auto-retried: the spend
    was real and a rerun may just spend it again — that's the operator's
    call."""

    code = "timeout"


class HarnessOverloadedError(HarnessError):
    code = "overloaded"
    retry = Retry.TRANSIENT
    retry_delays = (300, 900)


class HarnessRateLimitError(HarnessError):
    code = "rate_limited"
    retry = Retry.QUOTA


class HarnessUsageLimitError(HarnessError):
    code = "usage_limit"
    retry = Retry.QUOTA


class HarnessAuthError(HarnessError):
    code = "auth"


class HarnessInvalidOutputError(HarnessError):
    code = "invalid_output"


class HarnessSandboxError(HarnessError):
    """Transient because every attempt provisions or re-attaches its host,
    which covers both the unreachable and the gone-for-good VM."""

    code = "sandbox"
    retry = Retry.TRANSIENT
    retry_delays = (60, 300)


class HarnessSandboxProvisioningError(HarnessSandboxError):
    """A control-plane provisioning or transport failure while creating or
    re-attaching a host, or reaching a freshly created one.

    Transient on the same schedule as :class:`HarnessSandboxError` (it
    inherits ``retry``/``retry_delays``), but carries its own ``code`` so an
    exhausted retry records ``sandbox_provisioning`` rather than the generic
    ``sandbox`` classification — the failure taxonomy can then name the most
    transient failure class in the system instead of leaving it blank."""

    code = "sandbox_provisioning"


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

    code = "not_connected"


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

    code = "first_byte"
    retry = Retry.TRANSIENT
    # Two immediate retries: the failed attempt cost 90 seconds, not minutes,
    # and the wedge is CLI-local — waiting buys nothing.
    retry_delays = (0, 0)

class ServiceNotConnectedError(Exception):
    """No identity is connected for this service — the appliance has nothing to
    act as there. The message names the service so any refusal is actionable."""

    def __init__(self, service: str) -> None:
        self.service = service
        super().__init__(f"{service} is not connected — connect it in Settings → Services.")


class ServiceConnectError(Exception):
    """A rejected connect. The message is authored by the service's ``verify``
    and safe to show; it never quotes anything the operator pasted."""


class OauthExchangeError(Exception):
    """Completing an OAuth connect flow failed — an unknown or expired state,
    or a rejected code exchange. Nothing is stored on failure, so re-running
    the connect flow is always safe. ``context`` is the begun flow's stash
    when the state resolved, and empty when it did not."""

    def __init__(self, provider: str, reason: str, *, context: dict) -> None:
        super().__init__(f"OAuth exchange for {provider!r} failed: {reason}")
        self.provider = provider
        self.reason = reason
        self.context = context


class OauthRefreshError(Exception):
    """Minting an access token from a stored grant failed — the provider
    rejected the refresh token, the token endpoint is unreachable, or a
    concurrent refresh never freed the lock. Re-connecting replaces the
    grant."""

    def __init__(self, provider: str, reason: str) -> None:
        super().__init__(f"OAuth refresh for {provider!r} failed: {reason}")
        self.provider = provider
        self.reason = reason

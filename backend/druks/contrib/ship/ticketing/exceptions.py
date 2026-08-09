class LinearAPIError(Exception):
    """Raised when Linear's GraphQL endpoint returns a logical error.

    Distinct from ``httpx.HTTPError`` (transport / HTTP-status failures)
    so callers can catch both failure classes precisely.
    """


class JiraAPIError(Exception):
    """Jira REST returned a non-2xx response. Distinct from ``httpx.HTTPError``
    (transport) so callers can ``except (httpx.HTTPError, JiraAPIError)``."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class UnknownTicketError(Exception):
    """The tracker has no ticket under the given key. Each provider raises it
    from its own not-found signal, so callers can answer "that ticket doesn't
    exist" without knowing which provider spoke."""

    def __init__(self, key: str, tracker: str) -> None:
        super().__init__(f"{key} doesn't exist in {tracker}")
        self.key = key
        self.tracker = tracker

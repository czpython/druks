class LinearAPIError(Exception):
    """Raised when Linear's GraphQL endpoint returns a logical error.

    Distinct from ``httpx.HTTPError`` (transport / HTTP-status failures)
    so callers can catch both failure classes precisely.
    """


class JiraAPIError(Exception):
    """Jira REST returned a non-2xx response. Distinct from ``httpx.HTTPError``
    (transport) so callers can ``except (httpx.HTTPError, JiraAPIError)``."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class TrackerTicketNotFound(Exception):
    def __init__(self, tracker_name: str, ticket_key: str) -> None:
        self.tracker_name = tracker_name
        self.ticket_key = ticket_key
        super().__init__(f"{tracker_name} knows no {ticket_key}")


class TrackerStatusUnavailable(Exception):
    def __init__(self, tracker_name: str, ticket_key: str, status_name: str) -> None:
        self.tracker_name = tracker_name
        self.ticket_key = ticket_key
        self.status_name = status_name
        super().__init__(f"{tracker_name} cannot move {ticket_key} to status {status_name!r}.")

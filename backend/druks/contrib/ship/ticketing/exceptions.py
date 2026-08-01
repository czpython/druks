class LinearAPIError(Exception):
    """Raised when Linear's GraphQL endpoint returns a logical error.

    Distinct from ``httpx.HTTPError`` (transport / HTTP-status failures)
    so callers can catch both failure classes precisely.
    """


class JiraAPIError(Exception):
    """Jira REST returned a non-2xx response. Distinct from ``httpx.HTTPError``
    (transport) so callers can ``except (httpx.HTTPError, JiraAPIError)``."""

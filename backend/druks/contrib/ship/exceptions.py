from druks.api.exceptions import AgentApiError


class TicketNotFound(AgentApiError):
    status_code = 404
    code = "TICKET_NOT_FOUND"

    def __init__(self, ticket: str, tracker: str) -> None:
        super().__init__(f"{ticket} doesn't exist in {tracker}")


class TrackerNotConfigured(AgentApiError):
    # The operator's problem, not the caller's: no tracker is selected or its
    # credentials are missing, so ship cannot reach a ticket at all.
    status_code = 503
    code = "TRACKER_NOT_CONFIGURED"

    def __init__(self) -> None:
        super().__init__(
            "No ticket tracker is configured — select Linear or Jira in the ship "
            "settings and connect its identity in Settings → Services."
        )

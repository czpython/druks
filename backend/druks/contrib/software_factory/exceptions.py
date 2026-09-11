from druks.api.exceptions import AgentApiError


class PrefixTakenError(Exception):
    def __init__(self, name: str) -> None:
        super().__init__(
            f"Druks cannot derive an unused ticket prefix from project name {name!r}. "
            "Choose another name."
        )


class TicketNotFound(AgentApiError):
    status_code = 404
    code = "TICKET_NOT_FOUND"

    def __init__(self, ticket: str, tracker: str) -> None:
        super().__init__(f"{ticket} doesn't exist in {tracker}")


class TrackerNotConfigured(AgentApiError):
    # The operator's problem, not the caller's: no tracker is selected or its
    # credentials are missing, so Software Factory cannot reach a ticket at all.
    status_code = 503
    code = "TRACKER_NOT_CONFIGURED"

    def __init__(self) -> None:
        super().__init__(
            "No ticket tracker is configured. Select one in the Software Factory settings. "
            "Linear and Jira also need their identity in Settings → Connections → Services."
        )

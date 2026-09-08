from druks.api.exceptions import AgentApiError


class ProjectNotFound(Exception):
    def __init__(self, project_id: int) -> None:
        super().__init__(f"project {project_id} does not exist")


class RepoNotFound(Exception):
    def __init__(self, repo_id: int) -> None:
        super().__init__(f"repo {repo_id} does not exist")


class InvalidPrefix(Exception):
    def __init__(self, prefix: str) -> None:
        super().__init__(f"project prefix {prefix!r} must be 2-6 letters A-Z")


class MissingPrefix(Exception):
    def __init__(self, name: str) -> None:
        super().__init__(
            f"project {name!r} has no ticket prefix — set one before minting identifiers"
        )


class PrefixLocked(Exception):
    def __init__(self, prefix: str) -> None:
        super().__init__(
            f"project prefix {prefix!r} has already minted tickets — the identifier "
            "namespace is fixed once a number has been handed out"
        )


class PrefixTaken(Exception):
    def __init__(self, prefix: str) -> None:
        super().__init__(f"project prefix {prefix!r} is already in use. Pick a different one.")


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
            "No ticket tracker is configured — select Linear or Jira in the Software Factory "
            "settings and connect its identity in Settings → Connections → Services."
        )

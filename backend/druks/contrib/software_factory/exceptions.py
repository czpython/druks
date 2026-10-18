from druks.api.exceptions import AgentApiError
from druks.exceptions import DruksError


class PrefixTakenError(DruksError):
    def __init__(self, name: str) -> None:
        super().__init__(
            f"Druks cannot derive an unused ticket prefix from project name {name!r}. "
            "Choose another name."
        )


class MergeConflictError(DruksError):
    """GitHub cannot merge the pull request, because its branch conflicts with the
    base branch. It keeps that base, so a replayed step raises the same conflict."""

    def __init__(self, base_ref: str, base_sha: str) -> None:
        super().__init__(f"The pull request conflicts with {base_ref} at {base_sha}.")
        self.base_ref = base_ref
        self.base_sha = base_sha


class TicketNotFound(AgentApiError):
    status_code = 404
    code = "TICKET_NOT_FOUND"

    def __init__(self, ticket: str, tracker: str) -> None:
        super().__init__(f"{ticket} doesn't exist in {tracker}")


class TicketNotMovable(AgentApiError):
    status_code = 409
    code = "TICKET_NOT_MOVABLE"


class TrackerRefused(AgentApiError):
    # The tracker failed the call: a revoked token, a status it does not know, or an outage.
    status_code = 502
    code = "TRACKER_REFUSED"

    def __init__(self, reason: str) -> None:
        super().__init__(f"The ticket tracker refused the request: {reason}")


class TrackerNotConfigured(AgentApiError):
    # The operator's problem, not the caller's: no tracker is selected or its
    # credentials are missing, so Software Factory cannot reach a ticket at all.
    status_code = 503
    code = "TRACKER_NOT_CONFIGURED"

    def __init__(self) -> None:
        super().__init__(
            "No ticket tracker is configured. Select one in the Software Factory settings. "
            "Linear, Jira, and GitHub also need their identity in "
            "Settings → Connections → Services."
        )


class RepoNotFound(AgentApiError):
    status_code = 404
    code = "REPO_NOT_FOUND"

    def __init__(self, repo_id: int) -> None:
        super().__init__(f"No project repo {repo_id}. Read the projects for the repos they hold.")


class AssigneeNotFound(AgentApiError):
    status_code = 404
    code = "ASSIGNEE_NOT_FOUND"

    def __init__(self, account_id: str) -> None:
        super().__init__(f"No account {account_id}.")

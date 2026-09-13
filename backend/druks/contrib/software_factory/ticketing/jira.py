from typing import Any

import httpx

from druks.core.apis.exceptions import JiraAPIError, UnknownTicketError
from druks.core.apis.jira import JiraClient

from .base import Tracker
from .enums import TicketStatus


class Jira(Tracker):
    known_exceptions = (JiraAPIError, UnknownTicketError, httpx.HTTPError)
    authority = "https://auth.atlassian.com"

    # These status names belong to the "Internal tools" issue type of druks-managed tickets.
    # Its transitions have no validators or required fields, so status moves work like
    # Linear's. A security issue differs: its Done gate requires a resolution and Fix
    # versions. The caller supplies BACKLOG (the resting status) and TRIGGER (the dispatch
    # trigger). Druks moves a ticket to the trigger only when asked to open a build.
    _STATIC_STATUS_NAMES: dict[TicketStatus, str] = {
        TicketStatus.IN_PROGRESS: "In Progress",
        # CR is code review: the PR is open and waits for a review.
        TicketStatus.IN_REVIEW: "Waiting CR",
        TicketStatus.DONE: "Done",
        # This workflow has no cancel state, so abandoned work closes as Done. Its Done
        # transition takes no resolution or Fix versions, so the move succeeds.
        TicketStatus.CANCELED: "Done",
    }

    def __init__(
        self,
        *,
        base_url: str,
        email: str,
        api_token: str,
        backlog_status: str = "",
        trigger_status: str = "",
        client: Any | None = None,
    ) -> None:
        self._client = JiraClient(
            base_url=base_url, email=email, api_token=api_token, client=client
        )
        self._status_names = dict(self._STATIC_STATUS_NAMES)
        # Empty leaves the operator-named statuses unmapped.
        if backlog_status:
            self._status_names[TicketStatus.BACKLOG] = backlog_status
        if trigger_status:
            self._status_names[TicketStatus.TRIGGER] = trigger_status

    async def set_status(self, key: str, status: TicketStatus) -> None:
        name = self._status_names.get(status)
        if not name:
            raise ValueError(f"Jira has no configured status name for {status}")
        await self._client.transition_issue(key, name)

    async def aclose(self) -> None:
        await self._client.aclose()

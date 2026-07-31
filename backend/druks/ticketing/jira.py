from typing import Any, ClassVar, Self

import httpx

from druks.core.apis.exceptions import JiraAPIError
from druks.core.apis.jira import JiraClient
from druks.settings import load_settings

from .base import Tracker
from .enums import TicketStatus
from .exceptions import TrackerNotConfigured

# These status names belong to the "Internal tools" issue type used for druks-managed tickets;
# its transitions have no validators or required fields, unlike security issues whose Done gate
# requires a resolution and Fix versions, so native status moves work like Linear's.
# READY_FOR_AGENT is supplied by the caller as the resting status; "Backlog" is the operator's
# dispatch trigger, so druks deliberately does not land there.
_STATIC_STATUS_NAMES: dict[TicketStatus, str] = {
    TicketStatus.IN_PROGRESS: "In Progress",
    TicketStatus.IN_REVIEW: "Waiting CR",  # CR = code review; PR open, awaiting review
    TicketStatus.DONE: "Done",
    # This workflow has no cancel state; abandoned work closes as Done (its Done
    # transition takes no resolution/Fix-versions fields, so the move succeeds).
    TicketStatus.CANCELED: "Done",
}


class Jira(Tracker):
    source = "jira"
    known_exceptions: ClassVar[tuple[type[BaseException], ...]] = (JiraAPIError, httpx.HTTPError)

    def __init__(
        self,
        *,
        base_url: str,
        email: str,
        api_token: str,
        status_names: dict[TicketStatus, str],
        client: Any | None = None,
    ) -> None:
        self._client = JiraClient(
            base_url=base_url, email=email, api_token=api_token, client=client
        )
        self._status_names = status_names

    @classmethod
    def from_settings(cls, *, ready_for_agent_status: str = "") -> Self:
        settings = load_settings()
        if not (settings.jira_base_url and settings.jira_email and settings.jira_api_token):
            raise TrackerNotConfigured("jira")
        names = dict(_STATIC_STATUS_NAMES)
        # Empty leaves READY_FOR_AGENT unmapped.
        if ready_for_agent_status:
            names[TicketStatus.READY_FOR_AGENT] = ready_for_agent_status
        return cls(
            base_url=settings.jira_base_url,
            email=settings.jira_email,
            api_token=settings.jira_api_token,
            status_names=names,
        )

    async def set_status(self, key: str, status: TicketStatus) -> None:
        name = self._status_names.get(status)
        if not name:
            raise ValueError(f"Jira has no configured status name for {status}")
        await self._client.transition_issue(key, name)

    async def aclose(self) -> None:
        await self._client.aclose()

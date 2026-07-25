from typing import Any, ClassVar, Self

import httpx

from druks.core.apis.exceptions import JiraAPIError
from druks.core.apis.jira import JiraClient
from druks.settings import load_settings

from .base import Tracker
from .datastructures import Ticket
from .enums import StatusKind, TicketStatus
from .exceptions import TrackerNotConfigured

# Jira's statusCategory.key → druks's normalized kind. Jira folds done+canceled
# into one "done" category, so canceled isn't distinguishable here.
_CATEGORY_KIND: dict[str, StatusKind] = {
    "new": StatusKind.BACKLOG,
    "indeterminate": StatusKind.STARTED,
    "done": StatusKind.DONE,
}

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

    def _normalize(self, raw: dict[str, Any]) -> Ticket:
        # Structured fields only — the engine reads these (status routing, repo
        # binding, label guards). Description + comments are left to the agent,
        # which reads the ticket itself; raw is kept for the write-side splice.
        fields = raw.get("fields") or {}
        status = fields.get("status") or {}
        category = (status.get("statusCategory") or {}).get("key", "")
        project = fields.get("project") or {}
        assignee = fields.get("assignee") or {}
        return Ticket(
            provider="jira",
            id=raw["id"],
            key=raw["key"],
            title=fields.get("summary") or "",
            url=f"{self._client.base_url}/browse/{raw['key']}",
            status_name=status.get("name"),
            status_kind=_CATEGORY_KIND.get(category, StatusKind.UNKNOWN),
            assignee_email=assignee.get("emailAddress"),
            assignee_name=assignee.get("displayName"),
            assignee_id=assignee.get("accountId"),
            project_name=project.get("name"),
            project_id=project.get("id"),
            labels=list(fields.get("labels") or []),
            # Jira creates sub-tasks + addresses labels by project key.
            container_id=project.get("key"),
            container_name=project.get("name"),
            raw=raw,
        )

    async def fetch_ticket(self, key: str) -> Ticket:
        return self._normalize(await self._client.get_issue(key))

    async def set_status(self, ticket: Ticket, status: TicketStatus) -> None:
        name = self._status_names.get(status)
        if not name:
            raise ValueError(f"Jira has no configured status name for {status}")
        await self._client.transition_issue(ticket.key, name)

    async def aclose(self) -> None:
        await self._client.aclose()

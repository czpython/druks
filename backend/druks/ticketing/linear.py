from typing import Any, ClassVar, Self

import httpx

from druks.core.apis.exceptions import LinearAPIError
from druks.core.apis.linear import LinearClient
from druks.settings import load_settings

from .base import Tracker
from .datastructures import Ticket
from .enums import StatusKind, TicketStatus
from .exceptions import TrackerNotConfigured

_STATE_KIND: dict[str, StatusKind] = {
    "backlog": StatusKind.BACKLOG,
    "unstarted": StatusKind.BACKLOG,
    "started": StatusKind.STARTED,
    "completed": StatusKind.DONE,
    "canceled": StatusKind.CANCELED,
    "triage": StatusKind.UNKNOWN,
}

# READY_FOR_AGENT is operator-set, so the caller supplies its name to
# from_settings(); the rest are fixed.
_STATIC_STATUS_NAMES: dict[TicketStatus, str] = {
    TicketStatus.IN_PROGRESS: "In Progress",
    TicketStatus.IN_REVIEW: "In Review",
    TicketStatus.DONE: "Done",
    TicketStatus.CANCELED: "Canceled",
}


class Linear(Tracker):
    source = "linear"
    known_exceptions: ClassVar[tuple[type[BaseException], ...]] = (LinearAPIError, httpx.HTTPError)

    def __init__(
        self,
        *,
        api_key: str,
        status_names: dict[TicketStatus, str],
        client: Any | None = None,
    ) -> None:
        self._client = LinearClient(api_key=api_key, client=client)
        self._status_names = status_names

    @classmethod
    def from_settings(cls, *, ready_for_agent_status: str = "") -> Self:
        settings = load_settings()
        if not settings.linear_api_key:
            raise TrackerNotConfigured("linear")
        names = dict(_STATIC_STATUS_NAMES)
        # Empty leaves READY_FOR_AGENT unmapped.
        if ready_for_agent_status:
            names[TicketStatus.READY_FOR_AGENT] = ready_for_agent_status
        return cls(api_key=settings.linear_api_key, status_names=names)

    def _normalize(self, raw: dict[str, Any]) -> Ticket:
        # Structured fields for the engine, plus the markdown description the
        # write-side splice needs. The agent reads comments/prose itself.
        state = raw.get("state") or {}
        project = raw.get("project") or {}
        team = raw.get("team") or {}
        assignee = raw.get("assignee") or {}
        labels = [n["name"] for n in (raw.get("labels") or {}).get("nodes", [])]
        return Ticket(
            provider="linear",
            id=raw["id"],
            key=raw["identifier"],
            title=raw.get("title") or "",
            description=raw.get("description"),
            url=raw.get("url"),
            status_name=state.get("name"),
            status_kind=_STATE_KIND.get(state.get("type", ""), StatusKind.UNKNOWN),
            priority=raw.get("priority"),
            assignee_email=assignee.get("email"),
            assignee_name=assignee.get("name"),
            assignee_id=assignee.get("id"),
            project_name=project.get("name"),
            project_id=project.get("id"),
            labels=labels,
            container_id=team.get("id"),
            container_name=team.get("name"),
            raw=raw,
        )

    async def fetch_ticket(self, key: str) -> Ticket:
        return self._normalize(await self._client.get_issue(key))

    async def set_status(self, ticket: Ticket, status: TicketStatus) -> None:
        name = self._status_names.get(status)
        if not name:
            raise ValueError(f"Linear has no configured status name for {status}")
        await self._client.update_issue_status(ticket.id, name)

    async def aclose(self) -> None:
        await self._client.aclose()

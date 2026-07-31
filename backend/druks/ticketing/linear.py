from typing import Any, ClassVar, Self

import httpx

from druks.core.apis.exceptions import LinearAPIError
from druks.core.apis.linear import LinearClient
from druks.settings import load_settings

from .base import Tracker
from .enums import TicketStatus
from .exceptions import TrackerNotConfigured

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

    async def set_status(self, key: str, status: TicketStatus) -> None:
        name = self._status_names.get(status)
        if not name:
            raise ValueError(f"Linear has no configured status name for {status}")
        # The status mutation resolves the issue by identifier, so the key is
        # the id Linear wants.
        await self._client.update_issue_status(key, name)

    async def aclose(self) -> None:
        await self._client.aclose()

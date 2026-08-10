from typing import Any

import httpx

from druks.core.apis.exceptions import LinearAPIError, UnknownTicketError
from druks.core.apis.linear import LinearClient

from .base import Tracker
from .enums import TicketStatus


class Linear(Tracker):
    known_exceptions = (LinearAPIError, UnknownTicketError, httpx.HTTPError)

    # TRIGGER and BACKLOG are operator-named; the rest are fixed.
    _STATIC_STATUS_NAMES: dict[TicketStatus, str] = {
        TicketStatus.IN_PROGRESS: "In Progress",
        TicketStatus.IN_REVIEW: "In Review",
        TicketStatus.DONE: "Done",
        TicketStatus.CANCELED: "Canceled",
    }

    def __init__(
        self,
        *,
        api_key: str,
        backlog_status: str = "",
        trigger_status: str = "",
        client: Any | None = None,
    ) -> None:
        self._client = LinearClient(api_key=api_key, client=client)
        self._status_names = dict(self._STATIC_STATUS_NAMES)
        # Empty leaves the operator-named statuses unmapped.
        if backlog_status:
            self._status_names[TicketStatus.BACKLOG] = backlog_status
        if trigger_status:
            self._status_names[TicketStatus.TRIGGER] = trigger_status

    async def set_status(self, key: str, status: TicketStatus) -> None:
        name = self._status_names.get(status)
        if not name:
            raise ValueError(f"Linear has no configured status name for {status}")
        # The status mutation resolves the issue by identifier, so the key is
        # the id Linear wants.
        await self._client.update_issue_status(key, name)

    async def aclose(self) -> None:
        await self._client.aclose()

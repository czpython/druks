from typing import Any

import httpx

from druks.core.apis.exceptions import LinearAPIError, UnknownTicketError
from druks.core.apis.linear import LinearClient

from .base import Tracker
from .enums import TicketStatus


class Linear(Tracker):
    known_exceptions = (LinearAPIError, UnknownTicketError, httpx.HTTPError)
    authority = "https://mcp.linear.app"

    def __init__(
        self,
        *,
        api_key: str,
        status_names: dict[TicketStatus, str],
        client: Any | None = None,
    ) -> None:
        self._client = LinearClient(api_key=api_key, client=client)
        # An empty name leaves that status unmapped.
        self._status_names = {status: name for status, name in status_names.items() if name}

    async def set_status(self, key: str, status: TicketStatus) -> None:
        name = self._status_names.get(status)
        if not name:
            raise ValueError(f"Linear has no configured status name for {status}")
        # The status mutation resolves the issue by identifier, so the key is
        # the id Linear wants.
        await self._client.update_issue_status(key, name)

    async def list_status_choices(self) -> list[tuple[str, str]]:
        # Teams can share a state name. The first team's state type labels it.
        labels: dict[str, str] = {}
        for state in await self._client.list_workflow_states():
            labels.setdefault(state["name"], f"{state['name']} ({state['type']})")
        return list(labels.items())

    async def aclose(self) -> None:
        await self._client.aclose()

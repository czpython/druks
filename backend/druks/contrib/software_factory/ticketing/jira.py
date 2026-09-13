from typing import Any

import httpx

from druks.core.apis.exceptions import JiraAPIError, UnknownTicketError
from druks.core.apis.jira import JiraClient

from .base import Tracker
from .enums import TicketStatus


class Jira(Tracker):
    known_exceptions = (JiraAPIError, UnknownTicketError, httpx.HTTPError)
    authority = "https://auth.atlassian.com"

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
        # An empty name leaves that status unmapped.
        self._status_names = {status: name for status, name in status_names.items() if name}

    async def set_status(self, key: str, status: TicketStatus) -> None:
        name = self._status_names.get(status)
        if not name:
            raise ValueError(f"Jira has no configured status name for {status}")
        await self._client.transition_issue(key, name)

    async def list_status_choices(self) -> list[tuple[str, str]]:
        # Team-managed projects can repeat a status name. The first one labels it.
        labels: dict[str, str] = {}
        for status in await self._client.list_statuses():
            category = status["statusCategory"]["name"].lower()
            labels.setdefault(status["name"], f"{status['name']} ({category})")
        return list(labels.items())

    async def aclose(self) -> None:
        await self._client.aclose()

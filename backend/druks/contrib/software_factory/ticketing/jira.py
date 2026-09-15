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
        # Jira runs a self-transition as a real change: a history entry, watcher
        # notifications, and automation triggers. A ticket already at the status stays untouched.
        if await self._client.get_issue_status(key) != name:
            await self._client.transition_issue(key, name)

    async def list_status_choices(self) -> list[dict[str, str]]:
        statuses = await self._client.list_statuses()
        category_order = {"new": 0, "indeterminate": 1, "done": 2}
        statuses.sort(
            key=lambda status: (
                category_order.get(status["statusCategory"]["key"], 3),
                status["name"].casefold(),
                status["name"],
            )
        )
        choices = {}
        for status in statuses:
            choices.setdefault(
                status["name"],
                {
                    "value": status["name"],
                    "label": status["name"],
                    "group": status["statusCategory"]["name"],
                },
            )
        return list(choices.values())

    async def aclose(self) -> None:
        await self._client.aclose()

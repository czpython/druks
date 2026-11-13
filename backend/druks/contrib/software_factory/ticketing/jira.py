from typing import Any

import httpx

from druks.core.apis.exceptions import JiraAPIError, TicketNotMovableError, UnknownTicketError
from druks.core.apis.jira import JiraClient

from .base import Tracker
from .enums import TicketStatus


class Jira(Tracker):
    known_exceptions = (JiraAPIError, TicketNotMovableError, UnknownTicketError, httpx.HTTPError)
    authority = "https://auth.atlassian.com"

    def __init__(
        self,
        *,
        base_url: str,
        email: str,
        api_key: str,
        status_names: dict[TicketStatus, str],
        client: Any | None = None,
    ) -> None:
        super().__init__(status_names=status_names)
        self._client = JiraClient(base_url=base_url, email=email, api_key=api_key, client=client)

    async def get_ticket(self, key: str) -> dict:
        issue = await self._client.get_issue(key)
        fields = issue["fields"]
        # An unassigned issue has a null assignee. Privacy settings can hide the email.
        assignee = fields["assignee"] or {}
        return {
            "source": "jira",
            "ticket_id": issue["id"],
            "identifier": issue["key"],
            "status": fields["status"]["name"],
            "title": fields["summary"],
            "url": f"{self._client.base_url.rstrip('/')}/browse/{issue['key']}",
            "project_name": fields["project"]["name"],
            "labels": fields["labels"],
            "actor_id": None,
            "assignee_id": assignee.get("accountId"),
            "assignee_email": assignee.get("emailAddress"),
            "assignee_name": assignee.get("displayName"),
        }

    async def get_status_name(self, key: str) -> str:
        return await self._client.get_issue_status(key)

    async def set_status(self, key: str, status: TicketStatus) -> None:
        name = self._status_names.get(status)
        if not name:
            raise ValueError(f"Jira has no configured status name for {status}")
        # Jira runs a self-transition as a real change: a history entry, watcher
        # notifications, and automation triggers. A ticket already at the status stays untouched.
        if await self._client.get_issue_status(key) != name:
            await self._client.transition_issue(key, name)

    async def get_blocked_by(self, key: str) -> dict[str, bool]:
        links = await self._client.list_issue_links(key)
        # A link holds only the other issue. An inward issue of a Blocks link blocks this one.
        return {
            link["inwardIssue"]["key"]: (
                link["inwardIssue"]["fields"]["status"]["statusCategory"]["key"] != "done"
            )
            for link in links
            if link["type"]["name"] == "Blocks" and "inwardIssue" in link
        }

    async def get_blocking(self, key: str) -> list[dict]:
        links = await self._client.list_issue_links(key)
        # A link lacks the project, labels, and assignee, so each ticket is read whole.
        return [
            await self.get_ticket(link["outwardIssue"]["key"])
            for link in links
            if link["type"]["name"] == "Blocks" and "outwardIssue" in link
        ]

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

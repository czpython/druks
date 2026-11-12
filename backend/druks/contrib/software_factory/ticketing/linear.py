import httpx

from druks.contrib.software_factory.constants import LINEAR_CLOSED_STATE_TYPES
from druks.core.apis.exceptions import LinearAPIError, UnknownTicketError
from druks.core.apis.linear import LinearClient

from .base import Tracker
from .enums import TicketStatus


class Linear(Tracker):
    known_exceptions = (LinearAPIError, UnknownTicketError, httpx.HTTPError)
    authority = "https://mcp.linear.app"

    def __init__(self, *, client: LinearClient, status_names: dict[TicketStatus, str]) -> None:
        super().__init__(status_names=status_names)
        self._client = client

    async def get_ticket(self, key: str) -> dict:
        issue = await self._client.get_issue(key)
        # An issue outside a project has no project, and an unassigned one no assignee.
        project = issue["project"] or {}
        assignee = issue["assignee"] or {}
        return {
            "source": "linear",
            "ticket_id": issue["id"],
            "identifier": issue["identifier"],
            "status": issue["state"]["name"],
            "title": issue["title"],
            "url": issue["url"],
            "project_name": project.get("name"),
            "labels": [label["name"] for label in issue["labels"]["nodes"]],
            "team": issue["team"]["id"],
            "actor_id": None,
            "assignee_id": assignee.get("id"),
            "assignee_email": assignee.get("email"),
            "assignee_name": assignee.get("name"),
        }

    async def get_status_name(self, key: str) -> str:
        return await self._client.get_issue_status(key)

    async def set_status(self, key: str, status: TicketStatus) -> None:
        name = self._status_names.get(status)
        if not name:
            raise ValueError(f"Linear has no configured status name for {status}")
        # The status mutation resolves the issue by identifier, so the key is
        # the id Linear wants.
        await self._client.update_issue_status(key, name)

    async def get_blocked_by(self, key: str) -> dict[str, bool]:
        issue = await self._client.get_issue_relations(key)
        return {
            relation["issue"]["identifier"]: (
                relation["issue"]["state"]["type"] not in LINEAR_CLOSED_STATE_TYPES
            )
            for relation in issue["inverseRelations"]["nodes"]
            if relation["type"] == "blocks"
        }

    async def get_blocking(self, key: str) -> list[str]:
        issue = await self._client.get_issue_relations(key)
        return [
            relation["relatedIssue"]["identifier"]
            for relation in issue["relations"]["nodes"]
            if relation["type"] == "blocks"
        ]

    async def list_status_choices(self) -> list[dict[str, str]]:
        choices = {}
        for state in await self._client.list_workflow_states():
            choices.setdefault(
                state["name"],
                {
                    "value": state["name"],
                    "label": state["name"],
                    "group": state["type"],
                },
            )
        return sorted(
            choices.values(), key=lambda choice: (choice["group"], choice["label"].casefold())
        )

    async def aclose(self) -> None:
        await self._client.aclose()

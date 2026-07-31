from typing import Any

import httpx

from .base import Tracker
from .enums import TicketStatus
from .exceptions import JiraAPIError

_DEFAULT_TIMEOUT = httpx.Timeout(30.0, connect=5.0, write=10.0, pool=5.0)
_DEFAULT_LIMITS = httpx.Limits(max_connections=20, max_keepalive_connections=10)


class JiraClient:
    def __init__(
        self,
        *,
        base_url: str,
        email: str,
        api_token: str,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = client or httpx.AsyncClient(
            timeout=_DEFAULT_TIMEOUT,
            limits=_DEFAULT_LIMITS,
            auth=httpx.BasicAuth(email, api_token),
            headers={"Accept": "application/json", "Content-Type": "application/json"},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        response = await self._client.request(method, f"{self.base_url}{path}", json=json)
        if not response.is_success:
            raise JiraAPIError(f"{method} {path} -> {response.status_code}: {response.text[:300]}")
        if response.status_code == 204 or not response.content:
            return {}
        return response.json()

    async def transition_issue(self, key: str, status_name: str) -> None:
        # Jira moves status only via transitions: find the one whose target is
        # the requested status, then execute it.
        data = await self._request("GET", f"/rest/api/3/issue/{key}/transitions")
        transition_id = next(
            (
                transition["id"]
                for transition in data["transitions"]
                if transition["to"]["name"] == status_name
            ),
            None,
        )
        if not transition_id:
            raise JiraAPIError(f"{key} has no transition to status {status_name!r}")
        await self._request(
            "POST",
            f"/rest/api/3/issue/{key}/transitions",
            json={"transition": {"id": transition_id}},
        )


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
    known_exceptions = (JiraAPIError, httpx.HTTPError)

    def __init__(
        self,
        *,
        base_url: str,
        email: str,
        api_token: str,
        ready_for_agent_status: str = "",
        client: Any | None = None,
    ) -> None:
        self._client = JiraClient(
            base_url=base_url, email=email, api_token=api_token, client=client
        )
        self._status_names = dict(_STATIC_STATUS_NAMES)
        # Empty leaves READY_FOR_AGENT unmapped.
        if ready_for_agent_status:
            self._status_names[TicketStatus.READY_FOR_AGENT] = ready_for_agent_status

    async def set_status(self, key: str, status: TicketStatus) -> None:
        name = self._status_names.get(status)
        if not name:
            raise ValueError(f"Jira has no configured status name for {status}")
        await self._client.transition_issue(key, name)

    async def aclose(self) -> None:
        await self._client.aclose()

from typing import Any

import httpx

from .base import Tracker
from .enums import TicketStatus
from .exceptions import JiraAPIError, TrackerStatusUnavailable, TrackerTicketNotFound

_DEFAULT_TIMEOUT = httpx.Timeout(30.0, connect=5.0, write=10.0, pool=5.0)
_DEFAULT_LIMITS = httpx.Limits(max_connections=20, max_keepalive_connections=10)


class JiraClient:
    def __init__(
        self,
        *,
        base_url: str,
        email: str,
        api_token: str,
        tracker_name: str,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.tracker_name = tracker_name
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
            raise JiraAPIError(
                f"{method} {path} -> {response.status_code}: {response.text[:300]}",
                status_code=response.status_code,
            )
        if response.status_code == 204 or not response.content:
            return {}
        try:
            body = response.json()
        except ValueError as error:
            raise JiraAPIError(f"{method} {path} returned invalid JSON.") from error
        if not isinstance(body, dict):
            raise JiraAPIError(f"{method} {path} returned a non-object response.")
        return body

    async def transition_issue(self, key: str, status_name: str) -> bool:
        try:
            issue = await self._request("GET", f"/rest/api/3/issue/{key}?fields=status")
        except JiraAPIError as error:
            if error.status_code == 404:
                raise TrackerTicketNotFound(self.tracker_name, key) from error
            raise

        fields = issue.get("fields")
        current_status = fields.get("status") if isinstance(fields, dict) else None
        current_name = current_status.get("name") if isinstance(current_status, dict) else None
        if not isinstance(current_name, str):
            raise JiraAPIError(f"GET issue status for {key} returned malformed data.")
        if current_name == status_name:
            return False

        data = await self._request("GET", f"/rest/api/3/issue/{key}/transitions")
        transitions = data.get("transitions")
        if not isinstance(transitions, list):
            raise JiraAPIError(f"GET transitions for {key} returned malformed data.")
        transition_id = None
        for transition in transitions:
            if not isinstance(transition, dict) or not isinstance(transition.get("to"), dict):
                raise JiraAPIError(f"GET transitions for {key} returned malformed data.")
            candidate_id = transition.get("id")
            candidate_name = transition["to"].get("name")
            if (
                not isinstance(candidate_id, str)
                or not candidate_id
                or not isinstance(candidate_name, str)
                or not candidate_name
            ):
                raise JiraAPIError(f"GET transitions for {key} returned malformed data.")
            if candidate_name == status_name:
                transition_id = candidate_id
                break
        if not transition_id:
            raise TrackerStatusUnavailable(self.tracker_name, key, status_name)
        await self._request(
            "POST",
            f"/rest/api/3/issue/{key}/transitions",
            json={"transition": {"id": transition_id}},
        )
        return True


class Jira(Tracker):
    display_name = "Jira"
    known_exceptions = (*Tracker.known_exceptions, JiraAPIError, httpx.HTTPError)

    # These status names belong to the "Internal tools" issue type used for druks-managed
    # tickets; its transitions have no validators or required fields, unlike security issues
    # whose Done gate requires a resolution and Fix versions, so native status moves work
    # like Linear's. READY_FOR_AGENT is supplied by the caller as the resting status;
    # "Backlog" is the operator's dispatch trigger, so druks deliberately does not land there.
    _STATIC_STATUS_NAMES: dict[TicketStatus, str] = {
        TicketStatus.IN_PROGRESS: "In Progress",
        TicketStatus.IN_REVIEW: "Waiting CR",  # CR = code review; PR open, awaiting review
        TicketStatus.DONE: "Done",
        # This workflow has no cancel state; abandoned work closes as Done (its Done
        # transition takes no resolution/Fix-versions fields, so the move succeeds).
        TicketStatus.CANCELED: "Done",
    }

    def __init__(
        self,
        *,
        base_url: str,
        email: str,
        api_token: str,
        ready_for_agent_status: str = "",
        client: Any | None = None,
    ) -> None:
        super().__init__(ready_for_agent_status)
        self._client = JiraClient(
            base_url=base_url,
            email=email,
            api_token=api_token,
            tracker_name=self.display_name,
            client=client,
        )

    async def move_ticket(self, key: str, status_name: str) -> bool:
        return await self._client.transition_issue(key, status_name)

    async def aclose(self) -> None:
        await self._client.aclose()

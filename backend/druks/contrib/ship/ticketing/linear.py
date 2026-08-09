import hashlib
from typing import Any

import httpx

from .base import Tracker
from .enums import TicketStatus
from .exceptions import LinearAPIError, UnknownTicketError

LINEAR_GRAPHQL_URL = "https://api.linear.app/graphql"


def compute_delivery_key(
    headers: dict[str, str],
    raw_body: bytes,
    payload: dict[str, Any],
) -> str:
    delivery_id = headers.get("linear-delivery")
    if delivery_id:
        return delivery_id

    action = str(payload.get("action", ""))
    issue_data = payload.get("data", {})
    issue_id = str(issue_data.get("id", ""))
    updated_at = str(issue_data.get("updatedAt", ""))
    body_digest = hashlib.sha256(raw_body).hexdigest()[:16]
    composite = f"{action}:{issue_id}:{updated_at}:{body_digest}"
    return hashlib.sha256(composite.encode()).hexdigest()


# Granular timeouts: short connect/write phases, longer read for slow Linear
# responses, bounded pool wait so a saturated pool fails fast instead of
# stalling the request indefinitely.
_DEFAULT_TIMEOUT = httpx.Timeout(30.0, connect=5.0, write=10.0, pool=5.0)
_DEFAULT_LIMITS = httpx.Limits(max_connections=20, max_keepalive_connections=10)


class LinearClient:
    def __init__(
        self,
        *,
        api_key: str,
        api_url: str = LINEAR_GRAPHQL_URL,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.api_key = api_key
        self.api_url = api_url
        # One long-lived AsyncClient per LinearClient instance — pools
        # connections across the many GraphQL calls a single build run
        # makes. Tests inject a stub client; production builds the default.
        self._client = client or httpx.AsyncClient(
            timeout=_DEFAULT_TIMEOUT,
            limits=_DEFAULT_LIMITS,
            headers={
                "Authorization": api_key,
                "Content-Type": "application/json",
            },
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def update_issue_status(self, issue_id: str, status_name: str) -> dict[str, Any]:
        data = await self._execute(
            """
            query DruksIssueWorkflowStates($issueId: String!) {
              issue(id: $issueId) {
                id
                identifier
                state { id name }
                team {
                  states {
                    nodes { id name }
                  }
                }
              }
            }
            """,
            {"issueId": issue_id},
        )
        issue = data["issue"]
        # Linear answers an unknown identifier with a null issue, not an error.
        if issue is None:
            raise UnknownTicketError(issue_id, "Linear")
        current_status = issue["state"]["name"]
        if current_status == status_name:
            return {
                "identifier": issue["identifier"],
                "status": current_status,
                "changed": False,
            }

        status_id = _status_id_by_name(issue["team"]["states"]["nodes"], status_name)
        result = await self._execute(
            """
            mutation DruksIssueUpdateStatus($issueId: String!, $statusId: String!) {
              issueUpdate(id: $issueId, input: { stateId: $statusId }) {
                success
                issue {
                  identifier
                  state { name }
                }
              }
            }
            """,
            {"issueId": issue_id, "statusId": status_id},
        )
        issue_result = result["issueUpdate"]["issue"]
        return {
            "identifier": issue_result["identifier"],
            "status": issue_result["state"]["name"],
            "changed": bool(result["issueUpdate"]["success"]),
        }

    async def _execute(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        response = await self._client.post(
            self.api_url,
            json={"query": query, "variables": variables},
        )
        response.raise_for_status()
        body = response.json()
        errors = body.get("errors")
        if errors:
            raise LinearAPIError(f"Linear API returned errors: {errors}")

        data = body.get("data")
        if not isinstance(data, dict):
            raise LinearAPIError("Linear API response did not include data.")

        return data


def _status_id_by_name(states: list[dict[str, Any]], status_name: str) -> str:
    for state in states:
        if state["name"] == status_name:
            return state["id"]

    available = ", ".join(state["name"] for state in states)
    raise LinearAPIError(f"Linear status {status_name!r} was not found. Available: {available}")


class Linear(Tracker):
    known_exceptions = (LinearAPIError, UnknownTicketError, httpx.HTTPError)

    # READY_FOR_AGENT and TRIGGER are operator-named; the rest are fixed.
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
        ready_for_agent_status: str = "",
        trigger_status: str = "",
        client: Any | None = None,
    ) -> None:
        self._client = LinearClient(api_key=api_key, client=client)
        self._status_names = dict(self._STATIC_STATUS_NAMES)
        # Empty leaves the operator-named statuses unmapped.
        if ready_for_agent_status:
            self._status_names[TicketStatus.READY_FOR_AGENT] = ready_for_agent_status
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

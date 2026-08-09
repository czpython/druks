import hashlib
from typing import Any

import httpx

from .base import Tracker
from .enums import TicketStatus
from .exceptions import LinearAPIError, TrackerStatusUnavailable, TrackerTicketNotFound

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
        tracker_name: str,
        api_url: str = LINEAR_GRAPHQL_URL,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.api_key = api_key
        self.api_url = api_url
        self.tracker_name = tracker_name
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

    async def update_issue_status(self, ticket_key: str, status_name: str) -> bool:
        team_key, separator, issue_number_text = ticket_key.rpartition("-")
        if (
            not separator
            or not team_key
            or not issue_number_text.isdecimal()
            or int(issue_number_text) <= 0
        ):
            raise LinearAPIError(f"Invalid Linear ticket key: {ticket_key!r}.")

        data = await self._execute(
            """
            query DruksIssueWorkflowStates($teamKey: String!, $issueNumber: Float!) {
              issues(
                first: 1
                includeArchived: true
                filter: { team: { key: { eq: $teamKey } }, number: { eq: $issueNumber } }
              ) {
                nodes {
                  id
                  state { name }
                  team {
                    states {
                      nodes { id name }
                    }
                  }
                }
              }
            }
            """,
            {"teamKey": team_key, "issueNumber": float(issue_number_text)},
        )

        issues = data.get("issues")
        if not isinstance(issues, dict) or not isinstance(issues.get("nodes"), list):
            raise LinearAPIError("Linear issue query returned malformed data.")
        nodes = issues["nodes"]
        if not nodes:
            raise TrackerTicketNotFound(self.tracker_name, ticket_key)
        issue = nodes[0]
        if not isinstance(issue, dict):
            raise LinearAPIError("Linear issue query returned a malformed issue.")
        issue_id = issue.get("id")
        state = issue.get("state")
        if (
            not isinstance(issue_id, str)
            or not issue_id
            or not isinstance(state, dict)
            or not isinstance(state.get("name"), str)
        ):
            raise LinearAPIError("Linear issue query returned a malformed issue.")

        current_status = state["name"]
        if current_status == status_name:
            return False

        team = issue.get("team")
        if not isinstance(team, dict):
            raise LinearAPIError("Linear issue query returned malformed team states.")
        states = team.get("states")
        if not isinstance(states, dict) or not isinstance(states.get("nodes"), list):
            raise LinearAPIError("Linear issue query returned malformed team states.")

        status_id = None
        for candidate in states["nodes"]:
            if (
                not isinstance(candidate, dict)
                or not isinstance(candidate.get("id"), str)
                or not candidate["id"]
                or not isinstance(candidate.get("name"), str)
            ):
                raise LinearAPIError("Linear issue query returned malformed team states.")
            if candidate["name"] == status_name:
                status_id = candidate["id"]
                break
        if not status_id:
            raise TrackerStatusUnavailable(self.tracker_name, ticket_key, status_name)

        result = await self._execute(
            """
            mutation DruksIssueUpdateStatus($issueId: String!, $statusId: String!) {
              issueUpdate(id: $issueId, input: { stateId: $statusId }) {
                success
              }
            }
            """,
            {"issueId": issue_id, "statusId": status_id},
        )
        update = result.get("issueUpdate")
        if not isinstance(update, dict) or update.get("success") is not True:
            raise LinearAPIError("Linear issue update did not succeed.")
        return True

    async def _execute(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        response = await self._client.post(
            self.api_url,
            json={"query": query, "variables": variables},
        )
        try:
            body = response.json()
        except ValueError:
            body = None

        errors = body.get("errors") if isinstance(body, dict) else None
        if isinstance(errors, list) and errors:
            raise LinearAPIError(f"Linear API returned errors: {errors}")

        response.raise_for_status()
        if not isinstance(body, dict):
            raise LinearAPIError("Linear API response was not a JSON object.")
        data = body.get("data")
        if not isinstance(data, dict):
            raise LinearAPIError("Linear API response did not include data.")

        return data


class Linear(Tracker):
    display_name = "Linear"
    known_exceptions = (*Tracker.known_exceptions, LinearAPIError, httpx.HTTPError)

    # READY_FOR_AGENT is operator-named; the rest are fixed.
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
        client: Any | None = None,
    ) -> None:
        super().__init__(ready_for_agent_status)
        self._client = LinearClient(
            api_key=api_key,
            tracker_name=self.display_name,
            client=client,
        )

    async def move_ticket(self, key: str, status_name: str) -> bool:
        return await self._client.update_issue_status(key, status_name)

    async def aclose(self) -> None:
        await self._client.aclose()

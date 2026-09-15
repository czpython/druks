from typing import Any
from urllib.parse import quote

import httpx

from druks.core.apis.exceptions import JiraAPIError, UnknownTicketError

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
    ):
        response = await self._client.request(method, f"{self.base_url}{path}", json=json)
        if not response.is_success:
            raise JiraAPIError(
                f"{method} {path} -> {response.status_code}: {response.text[:300]}",
                status_code=response.status_code,
            )
        if response.status_code == 204 or not response.content:
            return {}
        return response.json()

    async def transition_issue(self, key: str, status_name: str) -> None:
        # Jira moves status only via transitions: find the one whose target is
        # the requested status, then execute it.
        try:
            data = await self._request(
                "GET", f"/rest/api/3/issue/{key}/transitions?expand=transitions.fields"
            )
        except JiraAPIError as error:
            # The transitions lookup 404s only when the issue itself is unknown.
            if error.status_code == 404:
                raise UnknownTicketError(key, "Jira") from error
            raise
        transition = next(
            (
                transition
                for transition in data["transitions"]
                if transition["to"]["name"] == status_name
            ),
            None,
        )
        if not transition:
            raise JiraAPIError(f"{key} has no transition to status {status_name!r}")
        body: dict[str, Any] = {"transition": {"id": transition["id"]}}
        resolution = transition.get("fields", {}).get("resolution", {})
        if resolution.get("required"):
            # A workflow can require a resolution on this transition. Druks prefers "Done".
            allowed = resolution["allowedValues"]
            chosen = next((value for value in allowed if value["name"] == "Done"), allowed[0])
            body["fields"] = {"resolution": {"id": chosen["id"]}}
        await self._request("POST", f"/rest/api/3/issue/{key}/transitions", json=body)

    async def list_statuses(self, *, project_key: str = ""):
        """Statuses in a project's workflows, or all active workflows when no project is set."""
        if project_key:
            groups = await self._request(
                "GET", f"/rest/api/3/project/{quote(project_key, safe='')}/statuses"
            )
            return [status for group in groups for status in group["statuses"]]
        return await self._request("GET", "/rest/api/3/status")

from typing import Any

import httpx

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
            (t["id"] for t in data["transitions"] if t["to"]["name"] == status_name),
            None,
        )
        if transition_id is None:
            raise JiraAPIError(f"{key} has no transition to status {status_name!r}")
        await self._request(
            "POST",
            f"/rest/api/3/issue/{key}/transitions",
            json={"transition": {"id": transition_id}},
        )

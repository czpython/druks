from typing import Any

import httpx

from druks.core.apis.exceptions import JiraAPIError, TicketNotMovableError, UnknownTicketError

_DEFAULT_TIMEOUT = httpx.Timeout(30.0, connect=5.0, write=10.0, pool=5.0)
_DEFAULT_LIMITS = httpx.Limits(max_connections=20, max_keepalive_connections=10)
# The site URL refuses a scoped token, such as a service account's.
_GATEWAY = "https://api.atlassian.com/ex/jira"
SERVICE_ACCOUNT_DOMAIN = "serviceaccount.atlassian.com"


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
        self._api_url: str | None = None
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
        if not self._api_url:
            tenant = await self._client.get(f"{self.base_url}/_edge/tenant_info")
            tenant.raise_for_status()
            self._api_url = f"{_GATEWAY}/{tenant.json()['cloudId']}"
        response = await self._client.request(method, f"{self._api_url}{path}", json=json)
        if not response.is_success:
            raise JiraAPIError(
                f"{method} {path} -> {response.status_code}: {response.text[:300]}",
                status_code=response.status_code,
            )
        if response.status_code == 204 or not response.content:
            return {}
        return response.json()

    async def get_myself(self) -> dict[str, Any]:
        return await self._request("GET", "/rest/api/3/myself")

    async def get_issue_status(self, key: str) -> str:
        try:
            issue = await self._request("GET", f"/rest/api/3/issue/{key}?fields=status")
        except JiraAPIError as error:
            if error.status_code == 404:
                raise UnknownTicketError(key, "Jira") from error
            raise
        return issue["fields"]["status"]["name"]

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
            reachable = ", ".join(transition["to"]["name"] for transition in data["transitions"])
            raise TicketNotMovableError(
                f"{key} has no transition to status {status_name!r}. "
                f"It can move to {reachable or 'no other status'}."
            )
        body: dict[str, Any] = {"transition": {"id": transition["id"]}}
        resolution = transition.get("fields", {}).get("resolution", {})
        if resolution.get("required"):
            # A workflow can require a resolution on this transition. Druks prefers "Done".
            allowed = resolution["allowedValues"]
            chosen = next((value for value in allowed if value["name"] == "Done"), allowed[0])
            body["fields"] = {"resolution": {"id": chosen["id"]}}
        await self._request("POST", f"/rest/api/3/issue/{key}/transitions", json=body)

    async def list_statuses(self):
        """The statuses of every active workflow. Browse projects is enough to read them."""
        return await self._request("GET", "/rest/api/3/status")

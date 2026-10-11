import httpx

from druks.browser.exceptions import BrowserCommandError


class PinchTab:
    """A borrowed browser, driven through the PinchTab bridge. Every method
    works in the tab that the first ``navigate`` opens."""

    def __init__(self, http: httpx.AsyncClient) -> None:
        self.http = http
        self.tab_id = ""

    async def navigate(self, url: str) -> None:
        reply = await self._send("POST", "/navigate", json={"url": url, "tabId": self.tab_id})
        self.tab_id = reply["tabId"]

    async def evaluate(self, expression: str):
        """The JSON value of ``expression`` in the page. A promise resolves first."""
        reply = await self._send(
            "POST",
            "/evaluate",
            json={"expression": expression, "awaitPromise": True, "tabId": self.tab_id},
        )
        return reply["result"]

    async def snapshot(self) -> dict:
        """The accessibility tree. Its refs name elements for ``action``."""
        return await self._send("GET", "/snapshot", params={"tabId": self.tab_id})

    async def text(self) -> str:
        reply = await self._send("GET", "/text", params={"tabId": self.tab_id})
        return reply["text"]

    async def action(self, kind: str, **fields) -> dict:
        """One PinchTab action, such as ``action("click", selector="#more")``."""
        return await self._send(
            "POST", "/action", json={"kind": kind, "tabId": self.tab_id, **fields}
        )

    async def _send(self, method: str, path: str, **request) -> dict:
        response = await self.http.request(method, path, **request)
        if response.is_success:
            return response.json()
        raise BrowserCommandError(path.removeprefix("/"), response.json()["error"])

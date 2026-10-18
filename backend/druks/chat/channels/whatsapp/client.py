import httpx

from .exceptions import NumberNotOnWhatsAppError, WahaError


class WahaClient:
    """WAHA's HTTP API under one key. The Services card's key creates and deletes a
    session and its key. A linked number's session key makes every other call."""

    def __init__(self, url: str, key: str, session_name: str = "") -> None:
        self.url = url.rstrip("/")
        self.key = key
        self.session_name = session_name

    async def request(
        self, method: str, path: str, *, accept: str = "*/*", **values
    ) -> httpx.Response:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=5.0)) as http:
            response = await http.request(
                method,
                f"{self.url}{path}",
                headers={"X-Api-Key": self.key, "Accept": accept},
                **values,
            )
        if response.is_success:
            return response
        raise WahaError(f"WAHA answered {method} {path} with HTTP {response.status_code}.")

    async def create_session(self) -> str:
        """Create and start a session. WAHA names it."""
        response = await self.request("POST", "/api/sessions", json={"start": True})
        return response.json()["name"]

    async def create_key(self, session_name: str) -> dict[str, str]:
        """A key that acts on this one session: its ``id`` and its secret ``key``."""
        response = await self.request("POST", "/api/keys", json={"session": session_name})
        return response.json()

    async def delete_session(self, session_name: str) -> None:
        await self.request("DELETE", f"/api/sessions/{session_name}")

    async def delete_key(self, key_id: str) -> None:
        await self.request("DELETE", f"/api/keys/{key_id}")

    async def logout(self) -> None:
        """Drop the session's linked device. WhatsApp then wants a new scan."""
        await self.request("POST", f"/api/sessions/{self.session_name}/logout")

    async def start(self) -> None:
        await self.request("POST", f"/api/sessions/{self.session_name}/start")

    async def configure(self, config: dict) -> None:
        """Replace the session's whole config. WAHA restarts the session."""
        await self.request("PUT", f"/api/sessions/{self.session_name}", json={"config": config})

    async def get_qr(self) -> dict[str, str]:
        """The QR code to link the number: its ``mimetype`` and base64 ``data``."""
        # WAHA answers JSON only to exactly this Accept value, and PNG bytes otherwise.
        response = await self.request(
            "GET", f"/api/{self.session_name}/auth/qr", accept="application/json"
        )
        return response.json()

    async def get_phone_chat_id(self, chat_id: str) -> str:
        """The chat ID of the person's phone number. WhatsApp can name a person by a
        LID instead, and WAHA maps it back when it knows the person."""
        if chat_id.endswith("@lid"):
            response = await self.request("GET", f"/api/{self.session_name}/lids/{chat_id}")
            return response.json()["pn"] or chat_id
        return chat_id

    async def get_chat_id_for_tag(self, digits: str) -> str:
        """The chat ID that @<digits> names in a message. WhatsApp names a person by a
        phone number or by a LID. The digits are a phone number when WAHA knows a LID for
        them, and a LID otherwise."""
        response = await self.request("GET", f"/api/{self.session_name}/lids/pn/{digits}")
        if response.json()["lid"]:
            return f"{digits}@c.us"
        return f"{digits}@lid"

    async def get_chat_id_for_phone(self, phone: str) -> str:
        """The chat ID of the WhatsApp account on a phone number."""
        response = await self.request(
            "GET",
            "/api/contacts/check-exists",
            params={"phone": phone, "session": self.session_name},
        )
        result = response.json()
        if result["numberExists"]:
            return result["chatId"]
        raise NumberNotOnWhatsAppError(f"{phone} has no WhatsApp account.")

    async def get_messages(self, chat_id: str, *, limit: int) -> list[dict]:
        """The chat's newest messages in the session's store, newest first, without media."""
        response = await self.request(
            "GET",
            f"/api/{self.session_name}/chats/{chat_id}/messages",
            params={
                "limit": limit,
                "sortBy": "timestamp",
                "sortOrder": "desc",
                "downloadMedia": "false",
            },
        )
        return response.json()

    async def get_group_name(self, group_id: str) -> str:
        # NOWEB calls a group's name its subject, and GOWS calls it Name.
        group = (await self.request("GET", f"/api/{self.session_name}/groups/{group_id}")).json()
        return group.get("subject") or group.get("Name") or ""

    async def new_message_id(self) -> str:
        response = await self.request("GET", f"/api/{self.session_name}/new-message-id")
        return response.json()["id"]

    async def send_text(
        self, chat_id: str, text: str, *, message_id: str, mentions: list[str]
    ) -> None:
        await self.request(
            "POST",
            "/api/sendText",
            json={
                "session": self.session_name,
                "chatId": chat_id,
                "text": text,
                "id": message_id,
                "mentions": mentions,
            },
        )

    async def download(self, path: str) -> bytes:
        return (await self.request("GET", path)).content

import httpx

from druks.core.apis.exceptions import TwilioError, TwilioNotFoundError


class TwilioClient:
    """Twilio's REST API for one account."""

    def __init__(self, account_sid: str, auth_token: str) -> None:
        self.url = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}"
        self.auth = httpx.BasicAuth(account_sid, auth_token)

    async def request(self, method: str, path: str, **values) -> dict:
        async with httpx.AsyncClient(
            auth=self.auth, timeout=httpx.Timeout(30.0, connect=5.0)
        ) as http:
            response = await http.request(method, f"{self.url}{path}", **values)
        if response.is_success:
            return response.json()
        message = f"Twilio answered {method} {path} with HTTP {response.status_code}."
        if response.status_code == 404:
            raise TwilioNotFoundError(message)
        raise TwilioError(message)

    async def get_account(self) -> dict:
        return await self.request("GET", ".json")

    async def list_numbers(self) -> list[dict]:
        """The account's numbers that send their calls to a voice URL. Twilio ignores the
        voice URL of a number that a TwiML App or a SIP trunk handles, so the list skips
        those. It reads one page, which holds up to 1,000 numbers."""
        page = await self.request("GET", "/IncomingPhoneNumbers.json", params={"PageSize": 1000})
        return [
            number
            for number in page["incoming_phone_numbers"]
            if number["capabilities"]["voice"]
            and not number["voice_application_sid"]
            and not number["trunk_sid"]
        ]

    async def get_number(self, sid: str) -> dict:
        return await self.request("GET", f"/IncomingPhoneNumbers/{sid}.json")

    async def set_voice_url(self, sid: str, url: str) -> None:
        """Point the number's calls at ``url``. An empty URL stops them."""
        await self.request(
            "POST",
            f"/IncomingPhoneNumbers/{sid}.json",
            data={"VoiceUrl": url, "VoiceMethod": "POST"},
        )

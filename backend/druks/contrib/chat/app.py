import httpx

from druks.agents import Agent
from druks.apps import App
from druks.contrib.chat.contracts import TurnOutput
from druks.doctor import CheckResult
from druks.settings import load_settings


async def check_appliance_mcp() -> CheckResult:
    """Whether this appliance's /mcp answers, so a Talk sandbox can operate
    as the signed-in operator. Skip when sandbox execution is off."""
    settings = load_settings()
    if not settings.sandbox.service_url:
        return CheckResult(
            name="appliance_mcp",
            ok=True,
            detail="skipped — sandbox execution is off",
        )
    endpoint = settings.urls.endpoint.rstrip("/")
    if not endpoint:
        return CheckResult(
            name="appliance_mcp",
            ok=False,
            pending=True,
            detail="urls.endpoint is unset — the sandbox needs it to reach /mcp.",
        )
    url = f"{endpoint}/mcp"
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(url)
    except httpx.RequestError as error:
        return CheckResult(
            name="appliance_mcp",
            ok=False,
            detail=f"{url} is unreachable: {error}. Chat Talk needs it.",
        )
    if response.status_code >= 500:
        return CheckResult(
            name="appliance_mcp",
            ok=False,
            detail=f"{url} returned {response.status_code}. Chat Talk needs it.",
        )
    return CheckResult(name="appliance_mcp", ok=True, detail=url)


class Chat(App):
    name = "chat"
    icon = "message-square"
    description = "Operator conversations this appliance owns — several threads, one account each."
    # Every table this app owns carries the ``chat_`` prefix, so the thread's
    # schema can never collide with core's or another app's.
    prefix_tables = True
    navigation = ["list"]
    checks = [check_appliance_mcp]

    reply = Agent(
        description="replies to the operator on one conversation turn",
        prompt="chat/talk.md",
        contract=TurnOutput,
    )

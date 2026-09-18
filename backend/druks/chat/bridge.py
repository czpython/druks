import asyncio
import json
import shlex
from pathlib import Path

import asyncssh
from acp.schema import SessionNotification
from pydantic import ValidationError

from druks.sandbox.host import Host
from druks.sandbox.layout import get_remote_home, get_work_root

from .constants import CHAT_BRIDGE_PORT
from .exceptions import ChatBridgeError, ChatBridgeUnavailable


class Bridge:
    def __init__(self, host: Host) -> None:
        self.host = host

    async def request(self, method: str, **values: object) -> dict:
        try:
            reader, writer = await self.host.open_tcp_connection("127.0.0.1", CHAT_BRIDGE_PORT)
        except asyncssh.ChannelOpenError as error:
            raise ChatBridgeUnavailable("The Chat bridge is not available.") from error
        try:
            writer.write((json.dumps({"method": method, **values}) + "\n").encode())
            await writer.drain()
            async with asyncio.timeout(120):
                line = await reader.readline()
        finally:
            writer.close()
            await writer.wait_closed()
        try:
            response = json.loads(line)
        except ValueError as error:
            raise ChatBridgeError(
                "The Chat bridge closed the request without an answer."
            ) from error
        if response["ok"]:
            return response
        raise ChatBridgeError(response["error"])

    async def start(self) -> None:
        """Upload and start the bridge in the sandbox unless it already answers."""
        if await self.is_running():
            return
        script = f"{get_remote_home(self.host.ssh_username)}/druks-chat-bridge.mjs"
        await self.host.upload_file(local=Path(__file__).with_name("bridge.mjs"), remote=script)
        root = shlex.quote(get_work_root(self.host.ssh_username))
        result = await self.host.exec(
            [
                "sh",
                "-c",
                f"mkdir -p {root} && nohup setsid node {shlex.quote(script)} {CHAT_BRIDGE_PORT} "
                f">{root}/chat-bridge.log 2>&1 </dev/null &",
            ],
            timeout=10,
        )
        if not result.ok:
            raise ChatBridgeError("The Chat bridge did not start.")
        async with asyncio.timeout(10):
            while not await self.is_running():
                await asyncio.sleep(0.25)

    async def is_running(self) -> bool:
        try:
            await self.request("ping")
        except ChatBridgeUnavailable:
            return False
        return True

    async def events(self, conversation_id: str, after: int) -> list[dict]:
        response = await self.request("events", conversationId=conversation_id, after=after)
        events = response["events"]
        try:
            notifications = [
                SessionNotification.model_validate(event["notification"]) for event in events
            ]
        except ValidationError as error:
            raise ChatBridgeError("The adapter returned an invalid ACP event.") from error
        else:
            for event, notification in zip(events, notifications, strict=True):
                event["notification"] = notification.model_dump(by_alias=True, exclude_unset=True)
        return events

    async def reply(self, conversation_id: str, message_id: str) -> tuple[str, list[dict]]:
        """The turn's reply text and the tool calls it made."""
        body = ""
        tools: dict[str, dict] = {}
        after = 0
        while events := await self.events(conversation_id, after):
            for event in events:
                if event["messageId"] == message_id:
                    update = event["notification"]["update"]
                    kind = update["sessionUpdate"]
                    if kind == "agent_message_chunk" and update["content"]["type"] == "text":
                        body += update["content"]["text"]
                    elif kind == "tool_call":
                        tools[update["toolCallId"]] = {**update, "textOffset": len(body)}
                    elif kind == "tool_call_update" and (tool := tools.get(update["toolCallId"])):
                        # The adapter can finish an earlier turn's tool call after a Stop.
                        tool.update(
                            {
                                key: value
                                for key, value in update.items()
                                if value is not None or key in ("rawInput", "rawOutput")
                            }
                        )
            after = events[-1]["sequence"]
        return body, list(tools.values())

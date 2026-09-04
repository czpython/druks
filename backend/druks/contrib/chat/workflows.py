from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Any, Literal

from druks.accounts.models import OperatorToken
from druks.contrib.chat.app import Chat
from druks.contrib.chat.enums import Role
from druks.contrib.chat.models import Conversation
from druks.mcp.constants import THIS_APPLIANCE
from druks.sandbox.datastructures import RequiredMcpServer
from druks.workflows import FatalError, Gate, Workflow, step
from druks.workspaces import Workspace, this_appliance_mcp_url

if TYPE_CHECKING:
    from druks.sandbox.host import Host

_WRITES = {
    "propose": "deny",
    "confirm": "defer",
    "full": "allow",
}


class ChatTurn(Gate):
    """The operator's next line, or a stop. Not ``review()`` — those controls
    would offer approve/request_changes on a chat turn."""

    name = "chat_turn"
    action: Literal["send", "stop"]
    note: str = ""


class ConfirmTool(Gate):
    """The operator approves or skips the mutating MCP call the agent proposed.
    Parks after the agent step and before the next ChatTurn — a run holds one
    gate at a time."""

    name = "confirm_tool"
    action: Literal["approve", "reject"]


@dataclass(frozen=True, kw_only=True)
class TalkWorkspace(Workspace):
    account_id: str
    run_id: str
    writes: str

    async def get_required_mcp_servers(self, **kwargs: Any) -> tuple[RequiredMcpServer, ...]:
        token = await OperatorToken.mint(
            account_id=self.account_id,
            agent_call_id=kwargs["call_id"],
            run_id=self.run_id,
            writes=self.writes,
        )
        return (
            RequiredMcpServer(
                name=THIS_APPLIANCE,
                url=this_appliance_mcp_url(self.host),
                token=token,
            ),
        )

    async def run_agent(self, *, account_id: str | None, **kwargs: Any):
        try:
            return await super().run_agent(account_id=account_id, **kwargs)
        finally:
            await OperatorToken.revoke(kwargs["call_id"])


class Talk(Workflow):
    """One conversation: agent reply, park, operator line, repeat until stop."""

    subject = Conversation
    steps_reuse_sandbox = True
    sandbox_hold = timedelta(minutes=15)
    workspace_class = TalkWorkspace

    async def run_multistep(self) -> None:
        while True:
            conversation = await self.subject
            messages = await conversation.list_prompt_messages()
            result = await Chat.reply(
                autonomy=conversation.autonomy,
                messages=[{"role": message.role, "body": message.body} for message in messages],
            )
            await self.record_message(Role.ASSISTANT, result.text)
            await self.name_thread()
            deferred = await self.deferred_writes()
            if deferred:
                decision = await ConfirmTool.wait(
                    input_request={
                        "presentation": "in_app",
                        "label": "Confirm the proposed action",
                        "controls": ["approve", "reject"],
                        "questions": [],
                    },
                    hold_sandbox=self.sandbox_hold,
                )
                if decision.action == "approve":
                    await self.apply_deferred_writes(deferred)
            reply = await ChatTurn.wait(
                input_request={
                    "presentation": "in_app",
                    "label": "Message",
                    "controls": ["send", "stop"],
                    "questions": [],
                },
                hold_sandbox=self.sandbox_hold,
            )
            if reply.action == "stop":
                return
            await self.record_message(Role.USER, reply.note)

    async def get_workspace_kwargs(self, host: "Host") -> dict[str, Any]:
        conversation = await self.subject
        if not self.account_id:
            raise FatalError("Talk runs as the operator who started the conversation.")
        return {
            **await super().get_workspace_kwargs(host),
            "account_id": self.account_id,
            "run_id": self.workflow_id,
            "writes": _WRITES[conversation.autonomy],
        }

    @step
    async def record_message(self, role: Role, body: str) -> None:
        conversation = await self.subject
        await conversation.add_message(role=role, body=body)

    @step
    async def name_thread(self) -> None:
        conversation = await self.subject
        await conversation.name_from_first_line()

    @step
    async def deferred_writes(self) -> list[dict[str, str]]:
        return await OperatorToken.take_deferred(self.workflow_id)

    @step
    async def apply_deferred_writes(self, writes: list[dict[str, str]]) -> None:
        if not self.account_id:
            raise FatalError("Talk runs as the operator who started the conversation.")
        await OperatorToken.play_deferred(self.account_id, writes)

    @classmethod
    async def dispatch(cls, *, conversation: Conversation) -> str:
        # One Talk per conversation: start() already dedups live runs on the
        # subject, so a later line answers ChatTurn instead of starting again.
        return await cls.start(subject=conversation)

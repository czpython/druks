from datetime import timedelta
from typing import TYPE_CHECKING, Any, Literal

from druks.contrib.chat.app import Chat
from druks.contrib.chat.enums import Autonomy, Role
from druks.contrib.chat.models import Conversation
from druks.workflows import Gate, Workflow, step
from druks.workspaces import OperatorWrites

if TYPE_CHECKING:
    from druks.sandbox.host import Host

# The operator is reading the last line, so the next one usually comes soon.
_IDLE_HOLD = timedelta(minutes=15)
_WRITES = {
    Autonomy.PROPOSE: OperatorWrites.DENY,
    Autonomy.CONFIRM: OperatorWrites.DEFER,
    Autonomy.FULL: OperatorWrites.ALLOW,
}
_TURN = {
    "presentation": "in_app",
    "label": "Message",
    "controls": ["send", "stop"],
    "questions": [],
}
_CONFIRM = {
    "presentation": "in_app",
    "label": "The agent proposed an action",
    "controls": ["approve", "reject"],
    "questions": [],
}


class ChatTurn(Gate):
    """The operator's next line, or a stop."""

    name = "chat_turn"
    action: Literal["send", "stop"]
    note: str = ""


class ConfirmTool(Gate):
    """The operator's answer on the actions the agent proposed. A run holds one
    gate at a time, so it parks before the next line."""

    name = "confirm_tool"
    action: Literal["approve", "reject"]


class Talk(Workflow):
    """One conversation: agent reply, park, operator line, repeat until stop."""

    subject = Conversation
    steps_reuse_sandbox = True

    async def run_multistep(self) -> None:
        while True:
            conversation = await self.subject
            messages = await conversation.list_prompt_messages()
            result = await Chat.reply(
                autonomy=conversation.autonomy,
                messages=[{"role": message.role, "body": message.body} for message in messages],
            )
            await self.record_message(Role.ASSISTANT, result.text)
            proposed = await self.take_proposals()
            if proposed:
                decision = await ConfirmTool.wait(input_request=_CONFIRM, hold_sandbox=_IDLE_HOLD)
                outcome = await self.settle_proposals(decision.action, proposed)
                await self.record_message(Role.SYSTEM, outcome)
            reply = await ChatTurn.wait(input_request=_TURN, hold_sandbox=_IDLE_HOLD)
            if reply.action == "stop":
                return
            await self.record_message(Role.USER, reply.note)

    async def get_workspace_kwargs(self, host: "Host") -> dict[str, Any]:
        conversation = await self.subject
        return {
            **await super().get_workspace_kwargs(host),
            "operator_writes": _WRITES[conversation.autonomy],
        }

    @step
    async def record_message(self, role: Role, body: str) -> None:
        conversation = await self.subject
        await conversation.add_message(role=role, body=body)

    @step
    async def settle_proposals(self, action: str, proposed: list[dict[str, str]]) -> str:
        """Run what the operator approved and say what happened. A proposal that
        failed is the operator's to read, and the conversation carries on either
        way. Touches no row: applying a proposal calls this appliance back, and
        that call shares this task's database session."""
        if action == "approve":
            failures = await self.apply_proposals(proposed)
            ran = f"{len(proposed) - len(failures)} of {len(proposed)} approved actions ran."
            if failures:
                return f"{ran} Failed: " + "; ".join(failures)
            return ran
        return f"{len(proposed)} proposed actions did not run."

    @classmethod
    async def dispatch(cls, *, conversation: Conversation) -> str:
        return await cls.start(subject=conversation)

from datetime import timedelta

from druks.contrib.chat.app import Chat
from druks.contrib.chat.enums import Role
from druks.contrib.chat.models import Conversation
from druks.workflows import Gate, Workflow, step


class ChatTurn(Gate):
    """The operator's next line, or a stop. Not ``review()`` — those controls
    would offer approve/request_changes on a chat turn."""

    name = "chat_turn"
    text: str
    stop: bool = False


class Talk(Workflow):
    """One conversation: agent reply, park, operator line, repeat until stop."""

    subject = Conversation
    steps_reuse_sandbox = True
    sandbox_hold = timedelta(minutes=15)

    async def run_multistep(self) -> None:
        while True:
            conversation = await self.subject
            messages = await conversation.list_messages()
            result = await Chat.reply(
                autonomy=conversation.autonomy,
                messages=[{"role": message.role, "body": message.body} for message in messages],
            )
            await self.record_message(Role.ASSISTANT, result.text)
            reply = await ChatTurn.wait(
                input_request={"presentation": "in_app", "label": "Chat turn"},
                hold_sandbox=self.sandbox_hold,
            )
            if reply.stop:
                return
            await self.record_message(Role.USER, reply.text)

    @step
    async def record_message(self, role: Role, body: str) -> None:
        conversation = await self.subject
        await conversation.add_message(role=role, body=body)

    @classmethod
    async def dispatch(cls, *, conversation: Conversation) -> str:
        # One Talk per conversation: start() already dedups live runs on the
        # subject, so a later line answers ChatTurn instead of starting again.
        return await cls.start(subject=conversation)

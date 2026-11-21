from druks.apps import App
from druks.instructions import Bot, Instruction

from .contracts import AnswerOutput
from .enums import BotAccess


class Chat(App):
    """Live agent sessions per account, and the channels people reach them through.
    The package walk discovers each channel, with its services, webhooks, and routes."""

    name = "chat"
    icon = "messages-square"
    description = "Live agent conversations."
    builtin = True
    bot = Bot(prompt="chat/bot.md", access=BotAccess.PRIVATE)
    run_instruction = Instruction(
        description="does a task that a person gave in a chat, in a fresh box",
        prompt="chat/run_instruction.md",
        contract=AnswerOutput,
    )

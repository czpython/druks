from druks.agents.constants import PRIVATE_AGENT_MESSAGE
from druks.durable.engine import step_session
from druks.exceptions import PermissionDenied
from druks.workflows import FatalError, Workflow

from .app import Chat
from .models import Conversation


class RunInstruction(Workflow):
    """Does a task that a person gave in a chat, in a fresh box. The chat's agent does it
    with its instructions, its model, and its skills. The run reports the task and its
    answer to the chat."""

    async def run(self, instruction: str) -> dict[str, str]:
        async with step_session() as session:
            conversation = await session.get(Conversation, self.conversation_id)
        if not conversation:
            raise FatalError("The conversation was deleted with its agent.")
        agent = conversation.agent
        # The agent can change owner or turn private after the conversation began.
        if agent and not conversation.account.can_talk_to(agent):
            raise PermissionDenied(PRIVATE_AGENT_MESSAGE)
        answer = await Chat.run_instruction(
            agent=agent,
            agent_instructions=agent.instructions if agent else "",
            instruction=instruction,
        )
        # A schedule starts the run outside any turn, so the chat holds no call that names
        # this run. The task in the result tells the agent which ask the answer is for.
        return {"task": instruction, "answer": answer}

from druks.mcp.constants import DRUKS_SERVER_NAME

# The dot keeps the name outside NAME_PATTERN, so no registry server can share its vault row.
CHAT_KEY_NAME = f"{DRUKS_SERVER_NAME}.chat"
CHAT_BRIDGE_PORT = 43123
TRANSCRIPTION_TIMEOUT_SECONDS = 120.0
# The header an agent's MCP calls carry to name their conversation.
CONVERSATION_HEADER = "X-Druks-Conversation"
# Every agent's prompt ends with this, so the agent knows an internal message when it
# reads one.
INTERNAL_MESSAGES_PROMPT = (
    "Druks, the platform that runs you, also writes to you. Its messages have the form "
    "[Internal: ...]. The person never sees them. Say what the person must know in your "
    "own words."
)
# Druks tells a run's conversation how the run ended with one of these templates.
RESULT_MESSAGE = "[Internal: Run {run} ended. Its result:\n{result}]"
FAILURE_MESSAGE = (
    "[Internal: Run {run} failed: {failure}. Tell the person in one short line, with no "
    "error details, and do not retry it.]"
)
# The line between what a person typed and what they said in the same message.
VOICE_NOTE_MARKER = "[Voice note]"
TRANSCRIPTION_FAILED_MESSAGE = (
    "[Internal: Druks could not turn the person's voice note into text. Tell the person "
    "in one short line to write it instead.]"
)

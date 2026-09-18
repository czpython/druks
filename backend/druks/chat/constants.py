from druks.mcp.constants import DRUKS_SERVER_NAME

# The dot keeps the name outside NAME_PATTERN, so no registry server can share its vault row.
CHAT_KEY_NAME = f"{DRUKS_SERVER_NAME}.chat"
CHAT_BRIDGE_PORT = 43123
# The header an agent's MCP calls carry to name their conversation.
CONVERSATION_HEADER = "X-Druks-Conversation"
# Druks tells a run's conversation how the run ended with one of these templates.
RESULT_MESSAGE = "Druks: Run {run} ended. Its result:\n{result}"
FAILURE_MESSAGE = "Druks: Run {run} failed: {failure}"

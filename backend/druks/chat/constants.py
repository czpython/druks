import hashlib
from pathlib import Path

from druks.mcp.constants import DRUKS_SERVER_NAME

# The dot keeps the name outside NAME_PATTERN, so no registry server can share its vault row.
CHAT_KEY_NAME = f"{DRUKS_SERVER_NAME}.chat"
CHAT_BRIDGE_PORT = 43123
# A running bridge echoes the hash it was launched with, so a deployment finds the bridges
# of an older Druks.
BRIDGE_VERSION = hashlib.sha256(Path(__file__).with_name("bridge.mjs").read_bytes()).hexdigest()
# The bridge statuses with a live session and no running turn.
BRIDGE_SETTLED_STATUSES = ("idle", "replied", "cancelled")
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
# Each turn opens with the time, so the agent never guesses it.
TIME_MESSAGE = "[Internal: The time is {now} ({timezone}).]"
# A new web conversation is named from its first message, so the name can arrive while the
# first reply runs.
NAMING_PROMPT = (
    "Name this conversation in at most six words. Reply with the name only.\n\nMessage: {message}"
)
# Druks tells a run's conversation how the run ended with one of these templates.
RESULT_MESSAGE = "[Internal: Run {run} ended. Its result:\n{result}]"
NO_RESULT_MESSAGE = "[Internal: Run {run} ended with no result. Tell the person it is done.]"
NO_RESULT_ON_SUBJECT_MESSAGE = (
    "[Internal: Run {run} ended with no result. Its output is on its subject, {subject}. "
    "Read it there and tell the person what the run did.]"
)
FAILURE_MESSAGE = (
    "[Internal: Run {run} failed: {failure}. Tell the person in one short line, with no "
    "error details, and offer what they can do next. Do not retry it.]"
)
# A channel turn that ends with no answer gets this once; the next empty turn fails.
NO_ANSWER_MESSAGE = (
    "[Internal: Your turn ended with no answer for the person. Write the answer now from "
    "what you already did. Do not repeat a tool call you already made.]"
)
# The line between what a person typed and what they said in the same message.
VOICE_NOTE_MARKER = "[Voice note]"
# The person reads this, so it holds no error details.
DELIVERY_FAILED_MESSAGE = "The reply is unavailable. Send the message again to retry."
TRANSCRIPTION_FAILED_MESSAGE = (
    "[Internal: Druks could not turn the person's voice note into text. Tell the person "
    "in one short line to write it instead.]"
)

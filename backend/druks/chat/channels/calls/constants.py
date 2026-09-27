from druks.mcp.constants import DRUKS_SERVER_NAME

# The dot keeps the name outside NAME_PATTERN, so no registry server can share its vault row.
CALLS_KEY_NAME = f"{DRUKS_SERVER_NAME}.calls"
MAX_CALL_SECONDS = 300
# The call ends when the caller says nothing for this long.
NO_SPEECH_SECONDS = 15
# A call token outlives the longest call by a minute: the calls server reports the end
# after the call.
CALL_TOKEN_SECONDS = MAX_CALL_SECONDS + 60
# How many lines of the caller's earlier calls the voice model gets.
EARLIER_LINES = 20
# The voice model reads this after the Bot's prompt.
CALL_PROMPT = (
    "You answer a phone call. Speak in short sentences. Never use lists, links, or "
    "formatting. Your first words say that you are an automated assistant and that the "
    'call is transcribed. Say "one moment" before a tool that takes time. When your tools '
    "cannot answer, say so, and offer only what your tools can do. To end the call, say "
    "goodbye, and then call end_call. Never read an internal message aloud."
)

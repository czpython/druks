# A Slack markdown block holds at most this many characters.
REPLY_PIECE_CHARACTERS = 12_000
# An unlinked person's link, and the message it holds, live this long.
LINK_TTL_SECONDS = 600
LINK_KEY = "chat:slack:link:{token}"
# A Slack file's Druks copy is found by Slack file id for this long.
FILE_KEY = "chat:slack:file:{file_id}"
FILE_TTL_SECONDS = 24 * 60 * 60
# A person's untagged replies in a thread reach their agent while the last message
# of their conversation there is younger than this.
JOINED_THREAD_SECONDS = 24 * 60 * 60
# How much of a thread the agent reads, and how many of its newest files it can fetch.
THREAD_MESSAGES = 200
THREAD_FILES = 20
LINK_MESSAGE = "Connect your Slack account to Druks, and I answer your message: {url}"
UNLINKED_REPLY = (
    "<@{user}>, your Slack account is not connected to Druks yet. "
    "I sent you a direct message with the link to connect it."
)

# A Slack markdown block holds at most this many characters.
REPLY_PIECE_CHARACTERS = 12_000
# An unlinked person's link, and the message it holds, live this long.
LINK_TTL_SECONDS = 600
LINK_KEY = "chat:slack:link:{token}"
# A person's untagged replies in a thread reach their agent while the last message
# of their conversation there is younger than this.
JOINED_THREAD_SECONDS = 24 * 60 * 60
# How much of a thread the agent reads.
THREAD_MESSAGES = 200
LINK_MESSAGE = "Connect your Slack account to Druks, and I answer you here: {url}"

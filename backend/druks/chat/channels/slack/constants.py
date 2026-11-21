# An unlinked person's link, and the message it holds, live this long.
LINK_TTL_SECONDS = 600
LINK_KEY = "chat:slack:link:{token}"
# A person who stays unlinked hears the welcome again after this long.
WELCOME_TTL_SECONDS = 30 * 24 * 60 * 60
WELCOME_KEY = "chat:slack:welcome:{user}"
# A person's untagged replies in a thread reach their agent while the last message
# of their conversation there is younger than this.
JOINED_THREAD_SECONDS = 24 * 60 * 60
# How much of a thread the agent reads.
THREAD_MESSAGES = 200
LINK_MESSAGE = "Connect your Slack account to Druks, and I answer your message: {url}"
UNLINKED_REPLY = (
    "<@{user}>, your Slack account is not connected to Druks yet. "
    "I sent you a direct message with the link to connect it."
)
UNACCEPTED_AUTOMATION_REPLY = (
    "I do not answer this automation yet. A Druks admin can accept it here: {url}"
)
WELCOME_TEMPLATE = "chat/channels/slack/welcome.md"
# Slack's limits for a message of inputs, from the Block Kit reference.
MESSAGE_BLOCKS_LIMIT = 50
BLOCK_ID_LIMIT = 255
LABEL_LIMIT = 2000
HINT_LIMIT = 2000
PLACEHOLDER_LIMIT = 150
INITIAL_VALUE_LIMIT = 3000
OPTION_TEXT_LIMIT = 75
OPTION_VALUE_LIMIT = 150
OPTIONS_LIMITS = {
    "plain_text_input": 0,
    "static_select": 100,
    "multi_static_select": 100,
    "radio_buttons": 10,
    "checkboxes": 10,
}

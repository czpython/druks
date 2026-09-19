# The platform tools of a number's admin, beside the Bot's admin_tools.
ADMIN_TOOLS = ("answer_gate", "chat_resume_conversation")
# The admin's system prompt, a template of the chat app.
ADMIN_PROMPT = "chat/bots/admin.md"
ADMIN_CODE_TTL_SECONDS = 600
# A pause ends when the number's phone stays quiet in the chat for this long.
PAUSE_SECONDS = 4 * 60 * 60
PAUSE_TOPIC = "pause"

# Druks writes each internal message from one of these templates.
ADMIN_ADDED_MESSAGE = (
    "Druks: This person sent the admin code, so Druks sends them this number's questions "
    "from now on. Greet them and say what you can do for them."
)
PHONE_CONNECTED_MESSAGE = (
    "Druks: This person connected their phone to their Druks account. "
    "Greet them and say what you can do for them."
)
QUESTION_MESSAGE = (
    "Druks: Run {run} waits for a decision since {parked_at}. It is about {user_name} "
    "({user_id}). The request:\n{request}"
)
PAUSED_MESSAGE = (
    "Druks: Someone answers {user_name} ({user_id}) from the number's phone. The bot "
    "stays quiet in that chat until the phone is quiet for {hours} hours, or until you "
    "resume conversation {conversation}."
)
PHONE_MESSAGE = "Druks: The number's phone sent this message in the chat:\n{body}"

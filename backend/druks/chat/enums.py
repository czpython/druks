from enum import StrEnum


class BotAccess(StrEnum):
    """Who can write to a Bot's number and which account owns each conversation."""

    # Anyone writes under the number's bot account. The number has an admin.
    OPEN = "open"
    # Proven senders write under their paired operator accounts. There is no admin.
    PAIRED = "paired"


class ConversationSource(StrEnum):
    WEB = "web"
    WHATSAPP = "whatsapp"


class MessageRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"


class MessageState(StrEnum):
    """Where the person's message is in its turn. Replies carry no state."""

    PENDING = "pending"
    DELIVERED = "delivered"
    REPLIED = "replied"
    INTERRUPTED = "interrupted"
    CANCELLED = "cancelled"


class PauseSignal(StrEnum):
    """What Druks sends to a chat's open pause. With no signal, the pause ends when
    the phone stays quiet for long enough."""

    # The phone sent another message in the chat: the quiet time starts again.
    EXTEND = "extend"
    # The admin resumed the chat: the pause ends now.
    RESUME = "resume"

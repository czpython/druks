from enum import StrEnum


class BotAccess(StrEnum):
    """Who a Bot's number answers and which account owns each conversation."""

    # Anyone writes under the number's bot account. The number has an admin.
    PUBLIC = "public"
    # Only operators who paired their phone write, each under their own account. There
    # is no admin.
    PRIVATE = "private"


class ConversationSource(StrEnum):
    WEB = "web"
    WHATSAPP = "whatsapp"
    SLACK = "slack"
    GITHUB = "github"
    CALL = "call"


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
    FAILED = "failed"


class PauseSignal(StrEnum):
    """What Druks sends to a chat's open pause. With no signal, the pause ends when
    the phone stays quiet for long enough."""

    # The phone sent another message in the chat: the quiet time starts again.
    EXTEND = "extend"
    # The admin resumed the chat: the pause ends now.
    RESUME = "resume"

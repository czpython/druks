from enum import StrEnum


class ConversationSource(StrEnum):
    WEB = "web"


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

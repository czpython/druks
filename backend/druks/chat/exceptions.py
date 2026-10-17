from typing import ClassVar

from druks.exceptions import DruksError


class ChatError(DruksError):
    """Base for Chat service failures."""

    # Whether a delivery that raised it runs again.
    is_retryable: ClassVar[bool] = True


class ChatBridgeError(ChatError):
    """The bridge did not return a usable response."""


class ChatHarnessError(ChatError):
    """A person must change the Chat agent's settings or credentials."""

    is_retryable = False


class ChatSandboxGone(ChatError):
    """The account has no live Chat sandbox."""


class ChatBridgeUnavailable(ChatBridgeError):
    """The loopback listener is not available."""


class ChannelHasNoThreadsError(ChatError):
    """The conversation's channel has no thread to read."""


class ChannelStartsNoConversationsError(ChatError):
    """The conversation's channel cannot start a chat with a person."""


class TranscriptionError(ChatError):
    """Druks got no transcript for a voice note."""


class ReminderError(ChatError):
    """A reminder cannot be made as asked. The message says what to change."""

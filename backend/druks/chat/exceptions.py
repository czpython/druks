from druks.exceptions import DruksError


class ChatError(DruksError):
    """Base for Chat service failures."""


class ChatBridgeError(ChatError):
    """The bridge did not return a usable response."""


class ChatHarnessError(ChatError):
    """The settings select a harness with no ACP adapter for Chat."""


class ChatSandboxGone(ChatError):
    """The account has no live Chat sandbox."""


class ChatBridgeUnavailable(ChatBridgeError):
    """The loopback listener is not available."""


class ChannelHasNoThreadsError(ChatError):
    """The conversation's channel has no thread to read."""

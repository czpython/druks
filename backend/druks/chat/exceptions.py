from druks.exceptions import DruksError


class ChatError(DruksError):
    """Base for Chat service failures."""


class ChatBridgeError(ChatError):
    """The box bridge did not return a usable response."""


class ChatHarnessError(ChatError):
    """The shared settings do not select Claude for Chat."""


class ChatBoxGone(ChatError):
    """The account has no live box."""


class ChatBridgeUnavailable(ChatBridgeError):
    """The loopback listener is not available."""

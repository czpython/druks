from druks.chat.exceptions import ChatError


class WahaError(ChatError):
    """WAHA did not accept a call. The message names the call and WAHA's status."""


class WhatsAppLinkError(ChatError):
    """Druks cannot link the number: the owner has a live number, or WAHA has no
    address to post events to."""

from druks.chat.exceptions import ChatError


class CallsLinkError(ChatError):
    """Druks cannot link the number. It is linked already, or Druks has no public address
    that Twilio can send calls to."""

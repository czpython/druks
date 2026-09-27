from druks.chat.exceptions import ChatError


class TwilioError(ChatError):
    """Twilio did not accept a request. The message names the request and Twilio's status."""


class TwilioNotFoundError(TwilioError):
    """The Twilio account holds no such resource."""


class CallsLinkError(ChatError):
    """Druks cannot link the number: a live connection holds it, or Twilio has no address
    to send its calls to."""

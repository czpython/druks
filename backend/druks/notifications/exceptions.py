from druks.exceptions import DruksError


class NotificationError(DruksError):
    pass


class UnknownTokenError(NotificationError):
    # The token is the respond capability handle — never echo it.
    def __init__(self):
        super().__init__("Unknown respond token.")


class InvalidChoiceError(NotificationError):
    pass


class AnswerNotAllowedError(NotificationError):
    pass


class AlreadyAcknowledgedError(NotificationError):
    def __init__(self):
        super().__init__("This notification was already acknowledged.")


class StaleRoundError(NotificationError):
    def __init__(self):
        super().__init__("The run has moved past the round this notification was sent for.")


class DisabledDestinationError(NotificationError):
    def __init__(self, name: str):
        super().__init__(f"Destination {name!r} is disabled; nothing was sent.")
        self.name = name


class DeliveryError(NotificationError):
    # The message lands in logs and stored failure reasons, so it carries only a
    # short reason.
    def __init__(self, reason: str):
        super().__init__(f"Delivery failed: {reason}.")


class ChannelUnavailableError(DeliveryError):
    """Slack refuses every post to the channel until someone fixes it in Slack: the bot is
    not in the channel, or the channel is archived or gone."""


class MalformedButtonError(NotificationError):
    # A button action_id embeds the respond token, so the message never echoes
    # the input.
    def __init__(self):
        super().__init__("Malformed button action_id.")

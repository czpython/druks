from druks.exceptions import DruksError


class NotificationError(DruksError):
    pass


class UnknownTokenError(NotificationError):
    # The token is the respond capability handle — never echo it.
    def __init__(self):
        super().__init__("Unknown respond token.")


class InvalidChoiceError(NotificationError):
    pass


class AlreadyAcknowledgedError(NotificationError):
    def __init__(self):
        super().__init__("This notification was already acknowledged.")


class StaleRoundError(NotificationError):
    def __init__(self):
        super().__init__("The run has moved past the round this notification was sent for.")


class CorruptCorrelationError(NotificationError):
    def __init__(self, notification_id: str, run_id: str):
        super().__init__(
            f"Notification {notification_id} references run {run_id}, which does not exist."
        )


class UnknownDestinationKindError(NotificationError):
    def __init__(self, kind: str, supported: tuple[str, ...]):
        super().__init__(f"Unknown destination kind {kind!r}; supported: {', '.join(supported)}.")
        self.kind = kind


class DisabledDestinationError(NotificationError):
    def __init__(self, name: str):
        super().__init__(f"Destination {name!r} is disabled; nothing was sent.")
        self.name = name


class DeliveryError(NotificationError):
    # The message lands in logs and stored failure reasons, so it carries only
    # the destination name and a short reason — never the webhook URL, which is
    # the credential.
    def __init__(self, name: str, reason: str):
        super().__init__(f"Delivery to destination {name!r} failed: {reason}.")
        self.name = name


class MalformedButtonError(NotificationError):
    # A button action_id embeds the respond token, so the message never echoes
    # the input.
    def __init__(self):
        super().__init__("Malformed button action_id.")

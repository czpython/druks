from druks.exceptions import DruksError


class InvalidWebhookError(DruksError):
    """Raised when a request path doesn't match any registered webhook."""

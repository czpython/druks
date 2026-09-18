from enum import StrEnum


class SecretKind(StrEnum):
    STATIC = "static"
    OAUTH = "oauth"
    APP_KEY = "app_key"
    SUBSCRIPTION = "subscription"
    SESSION = "session"


class IdentityStatus(StrEnum):
    RESOLVED = "resolved"
    UNAVAILABLE = "unavailable"
    FAILED = "failed"

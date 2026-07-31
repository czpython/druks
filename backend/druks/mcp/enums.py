from enum import StrEnum


class IdentityMode(StrEnum):
    SHARED = "shared"
    PER_USER = "per_user"


class TokenSource(StrEnum):
    STATIC = "static"
    STATIC_FROM_ENV = "static_from_env"
    OAUTH = "oauth"

from enum import StrEnum


class IdentityMode(StrEnum):
    SHARED = "shared"
    PER_USER = "per_user"


class TokenSource(StrEnum):
    STATIC = "static"
    OAUTH = "oauth"

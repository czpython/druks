from enum import StrEnum


class TokenSource(StrEnum):
    STATIC = "static"
    STATIC_FROM_ENV = "static_from_env"
    OAUTH = "oauth"

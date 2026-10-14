from enum import StrEnum


class BrowserSessionStatus(StrEnum):
    NEEDS_LOGIN = "needs_login"
    READY = "ready"
    STALE = "stale"
    ANONYMOUS = "anonymous"


class BrowserSessionPayloadFormat(StrEnum):
    STORAGE_STATE = "storage_state"
    PROFILE_DIR = "profile_dir"


class BrowserClient(StrEnum):
    """The client Druks starts beside Chrome to drive a borrow."""

    PINCHTAB = "pinchtab"
    PLAYWRIGHT = "playwright"

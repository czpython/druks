from enum import StrEnum


class BrowserSessionStatus(StrEnum):
    NEEDS_LOGIN = "needs_login"
    READY = "ready"
    STALE = "stale"
    ANONYMOUS = "anonymous"


class BrowserSessionPayloadFormat(StrEnum):
    STORAGE_STATE = "storage_state"
    PROFILE_DIR = "profile_dir"

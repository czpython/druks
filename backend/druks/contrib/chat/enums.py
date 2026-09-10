from enum import StrEnum


class Autonomy(StrEnum):
    """How far a conversation's next agent call may go. The setting is the
    conversation's, not the turn's, so a mode change applies to the next call."""

    PROPOSE = "propose"
    CONFIRM = "confirm"
    FULL = "full"


class Role(StrEnum):
    """Who wrote a line on the thread. Closed: a column can never hold a speaker
    no screen knows how to render."""

    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"

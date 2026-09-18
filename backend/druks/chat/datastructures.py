from dataclasses import dataclass


@dataclass(frozen=True)
class BotUser:
    """The person who writes in a conversation. ``id`` is always set; ``phone``
    is empty when the source hides the number."""

    id: str
    name: str
    phone: str
    source: str

from enum import Enum, StrEnum
from typing import Literal


class IdentityMode(StrEnum):
    SHARED = "shared"
    PER_USER = "per_user"


class Toolkit(Enum):
    """What a Druks key allows when it has no tool list: the whole API."""

    ALL = "all"


# The tools a Druks key allows. An empty tuple allows no tool.
AllowedTools = tuple[str, ...] | Literal[Toolkit.ALL]

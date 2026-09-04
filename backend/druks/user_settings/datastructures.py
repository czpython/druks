from typing import Literal, NamedTuple, get_args

Effort = Literal["low", "medium", "high"]
ALLOWED_EFFORTS: tuple[str, ...] = get_args(Effort)


class ResolvedChoice(NamedTuple):
    value: str
    source: Literal["agent", "default"]


class ResolvedTimeout(NamedTuple):
    value: int
    # declared: the agent's own timeout field.
    source: Literal["agent", "declared", "default"]

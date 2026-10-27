from typing import Literal, NamedTuple, get_args

# The levels Claude and Pi take as they are. Codex stops at xhigh and runs max as
# that; OpenCode ignores the level, its efforts being model variants.
Effort = Literal["low", "medium", "high", "xhigh", "max"]
ALLOWED_EFFORTS: tuple[str, ...] = get_args(Effort)


class ResolvedChoice(NamedTuple):
    value: str
    source: Literal["agent", "default"]


class ResolvedTimeout(NamedTuple):
    value: int
    # declared: the agent's own timeout field.
    source: Literal["agent", "declared", "default"]

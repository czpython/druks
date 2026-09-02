from typing import Literal, NamedTuple, get_args

Effort = Literal["low", "medium", "high"]
ALLOWED_EFFORTS: tuple[str, ...] = get_args(Effort)

# agent: per-agent override; declared: the agent's own field; harness: the
# harness default; default: the operator's one default model.
_SettingSource = Literal["agent", "declared", "harness"]


class ResolvedModel(NamedTuple):
    value: str
    source: Literal["agent", "default"]


class ResolvedEffort(NamedTuple):
    value: str
    source: Literal["agent", "harness"]


class ResolvedTimeout(NamedTuple):
    value: int
    source: _SettingSource

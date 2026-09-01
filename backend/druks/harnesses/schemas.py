from pydantic import ConfigDict, field_validator

from druks.schemas import Schema


class HarnessSummary(Schema):
    model_config = ConfigDict(from_attributes=True)

    name: str
    # A frozenset on the class; sorted so the wire is deterministic.
    login_kinds: list[str]

    @field_validator("login_kinds", mode="after")
    @classmethod
    def _sorted(cls, kinds: list[str]) -> list[str]:
        return sorted(kinds)

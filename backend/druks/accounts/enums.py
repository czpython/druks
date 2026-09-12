from enum import StrEnum


class OperatorWrites(StrEnum):
    """How far a call-scoped operator credential may go: read only, record a
    write as a proposal, or perform it. The credential carries the answer, and
    every door that accepts one reads it."""

    DENY = "deny"
    DEFER = "defer"
    ALLOW = "allow"

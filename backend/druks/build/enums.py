from enum import StrEnum


class ReviewDecision(StrEnum):
    APPROVE = "APPROVE"
    REQUEST_CHANGES = "REQUEST_CHANGES"
    COMMENT = "COMMENT"


class EvaluationVerdict(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    BLOCKED = "blocked"


class HumanFeedbackAction(StrEnum):
    NO_CHANGE = "no_change"
    CHANGE_REQUIRED = "change_required"
    CONTRACT_CHANGE_REQUIRED = "contract_change_required"
    QUESTION = "question"
    CLOSE = "close"


class HandoffStatus(StrEnum):
    SCOPED = "scoped"
    SHIPPED = "shipped"
    CANCELLED = "cancelled"

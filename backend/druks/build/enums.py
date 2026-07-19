from enum import StrEnum


class ReviewDecision(StrEnum):
    APPROVE = "APPROVE"
    APPROVE_WITH_REQUIRED_CHANGES = "APPROVE_WITH_REQUIRED_CHANGES"
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


class Outcome(StrEnum):
    FINISHED = "finished"
    CANCELLED = "cancelled"
    SCOPED = "scoped"


class HandoffStatus(StrEnum):
    SCOPED = "scoped"
    SHIPPED = "shipped"
    CANCELLED = "cancelled"

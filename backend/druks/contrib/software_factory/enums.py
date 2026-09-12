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


class Status(StrEnum):
    """The board's workflow. A ticket holds one of these and nothing else."""

    BACKLOG = "backlog"
    READY_FOR_AGENT = "ready_for_agent"
    IN_PROGRESS = "in_progress"
    BLOCKED = "blocked"
    IN_REVIEW = "in_review"
    DONE = "done"

    @property
    def label(self) -> str:
        return STATUS_LABELS[self]


# The stored value stays snake_case. Only these words change.
STATUS_LABELS: dict[Status, str] = {
    Status.BACKLOG: "Backlog",
    Status.READY_FOR_AGENT: "Ready for Agent",
    Status.IN_PROGRESS: "In Progress",
    Status.BLOCKED: "Blocked",
    Status.IN_REVIEW: "In Review",
    Status.DONE: "Done",
}


class Priority(StrEnum):
    NONE = "none"
    URGENT = "urgent"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"

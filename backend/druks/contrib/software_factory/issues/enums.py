from enum import StrEnum


class Status(StrEnum):
    """The board's workflow. Closed, so a column never holds a status no screen renders."""

    BACKLOG = "backlog"
    READY_FOR_AGENT = "ready_for_agent"
    IN_PROGRESS = "in_progress"
    BLOCKED = "blocked"
    IN_REVIEW = "in_review"
    DONE = "done"

    @property
    def label(self) -> str:
        return STATUS_LABELS[self]


# The stored value stays snake_case; only these words change.
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

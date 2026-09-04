from enum import StrEnum


class Status(StrEnum):
    """The board's workflow, closed on purpose: the enum *is* the workflow, so a
    column can never hold a status no screen knows how to render."""

    BACKLOG = "backlog"
    TODO = "todo"
    READY_FOR_AGENT = "ready_for_agent"
    IN_PROGRESS = "in_progress"
    IN_REVIEW = "in_review"
    DONE = "done"
    CANCELLED = "cancelled"

    @property
    def label(self) -> str:
        """What a column header or a chip spells this status as."""
        return STATUS_LABELS[self]


# Pinned display labels — the stored value stays snake_case forever; only these
# strings change when the board wants different words.
STATUS_LABELS: dict[Status, str] = {
    Status.BACKLOG: "Backlog",
    Status.TODO: "Todo",
    Status.READY_FOR_AGENT: "Ready for Agent",
    Status.IN_PROGRESS: "In Progress",
    Status.IN_REVIEW: "In Review",
    Status.DONE: "Done",
    Status.CANCELLED: "Cancelled",
}


class Priority(StrEnum):
    NONE = "none"
    URGENT = "urgent"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"

from enum import StrEnum


class Status(StrEnum):
    """The board's workflow, closed on purpose: the enum *is* the workflow, so a
    column can never hold a status no screen knows how to render."""

    BACKLOG = "backlog"
    READY_FOR_AGENT = "ready_for_agent"
    IN_PROGRESS = "in_progress"
    BLOCKED = "blocked"
    IN_REVIEW = "in_review"
    DONE = "done"

    @property
    def label(self) -> str:
        """What a column header or a chip spells this status as."""
        return STATUS_LABELS[self]

    @property
    def completed(self) -> bool:
        """The work got done. Funnel readers count this, not the display label."""
        return self is Status.DONE

    @property
    def terminal(self) -> bool:
        """Nothing moves out of here on its own. Terminal-ness lives on the
        enum, not on the display label: a board that renames a column has not
        changed its workflow, and every reader of ``ticket.transitioned`` reads
        this rather than guessing from a string."""
        return self is Status.DONE


# Pinned display labels — the stored value stays snake_case forever; only these
# strings change when the board wants different words.
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

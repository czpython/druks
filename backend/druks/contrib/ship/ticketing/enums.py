from enum import StrEnum


class TicketStatus(StrEnum):
    """The status druks asks a tracker to move a ticket to. Each provider maps
    these to whatever it calls them."""

    IN_PROGRESS = "in_progress"
    IN_REVIEW = "in_review"
    DONE = "done"
    CANCELED = "canceled"
    # Two operator-named statuses. TRIGGER opens a build (entering it fires the
    # intake webhook); BACKLOG is where a ticket rests when druks stops on it.
    TRIGGER = "trigger"
    BACKLOG = "backlog"

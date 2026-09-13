from enum import StrEnum


class TicketStatus(StrEnum):
    """A status that druks moves a tracker ticket to. Settings name it for each tracker."""

    IN_PROGRESS = "in_progress"
    IN_REVIEW = "in_review"
    DONE = "done"
    # TRIGGER opens a build. BACKLOG is where a ticket rests after druks stops work on it.
    TRIGGER = "trigger"
    BACKLOG = "backlog"

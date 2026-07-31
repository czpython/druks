from enum import StrEnum


class TicketStatus(StrEnum):
    """The status druks asks a tracker to move a ticket to. Each provider maps
    these to whatever it calls them."""

    IN_PROGRESS = "in_progress"
    IN_REVIEW = "in_review"
    DONE = "done"
    CANCELED = "canceled"
    READY_FOR_AGENT = "ready_for_agent"

from druks.contrib.issues.enums import Status
from druks.workflows import SubjectSummary


class TicketSummary(SubjectSummary):
    # The ticket's domain header — what only issues knows. ``label`` is the
    # identifier (``Ticket.get_label``), and the platform's subject read-side
    # composes this with the generic status and timeline.
    title: str
    status: Status

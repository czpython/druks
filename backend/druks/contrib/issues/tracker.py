from druks.contrib.issues.enums import Status
from druks.contrib.issues.models import Ticket
from druks.contrib.software_factory.ticketing.base import Tracker
from druks.contrib.software_factory.ticketing.enums import TicketStatus
from druks.core.apis.exceptions import UnknownTicketError

_BOARD = {
    TicketStatus.TRIGGER: Status.READY_FOR_AGENT,
    TicketStatus.BACKLOG: Status.BACKLOG,
    TicketStatus.CANCELED: Status.CANCELLED,
    TicketStatus.IN_PROGRESS: Status.IN_PROGRESS,
    TicketStatus.IN_REVIEW: Status.IN_REVIEW,
    TicketStatus.DONE: Status.DONE,
}


class IssuesTracker(Tracker):
    """Status writes the issues row. No credentials — Issues is this appliance."""

    known_exceptions = (UnknownTicketError,)

    async def set_status(self, key: str, status: TicketStatus) -> None:
        ticket = await Ticket.get_for_identifier(key)
        if not ticket:
            raise UnknownTicketError(key, "issues")
        await ticket.transition(_BOARD[status])

    async def aclose(self) -> None:
        return

from druks.contrib.software_factory.issues.enums import Status
from druks.contrib.software_factory.models import Ticket
from druks.contrib.software_factory.ticketing.base import Tracker
from druks.contrib.software_factory.ticketing.enums import TicketStatus
from druks.core.apis.exceptions import UnknownTicketError

_BOARD = {
    TicketStatus.TRIGGER: Status.READY_FOR_AGENT,
    TicketStatus.BACKLOG: Status.BACKLOG,
    TicketStatus.CANCELED: Status.DONE,
    TicketStatus.IN_PROGRESS: Status.IN_PROGRESS,
    TicketStatus.IN_REVIEW: Status.IN_REVIEW,
    TicketStatus.DONE: Status.DONE,
}


class IssuesTracker(Tracker):
    """Status writes the issues row. No credentials: the board is this appliance."""

    known_exceptions = (UnknownTicketError,)

    async def set_status(self, key: str, status: TicketStatus) -> None:
        if ticket := await Ticket.get_for_identifier(key):
            await ticket.transition(_BOARD[status])
            return
        raise UnknownTicketError(key, "issues")

    async def aclose(self) -> None:
        return

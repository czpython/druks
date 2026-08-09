from abc import ABC, abstractmethod

from .enums import TicketStatus
from .exceptions import TrackerStatusUnavailable, TrackerTicketNotFound


class Tracker(ABC):
    display_name: str
    _STATIC_STATUS_NAMES: dict[TicketStatus, str]
    # Transport and API errors a caller should expect and handle, so consumers
    # can `except tracker.known_exceptions` without importing provider types.
    known_exceptions: tuple[type[Exception], ...] = (
        TrackerTicketNotFound,
        TrackerStatusUnavailable,
    )

    def __init__(self, ready_for_agent_status: str = "") -> None:
        self._status_names = dict(self._STATIC_STATUS_NAMES)
        if ready_for_agent_status:
            self._status_names[TicketStatus.READY_FOR_AGENT] = ready_for_agent_status

    async def __aenter__(self) -> "Tracker":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

    async def set_status(self, key: str, status: TicketStatus) -> None:
        name = self._status_names.get(status)
        if not name:
            raise ValueError(f"{self.display_name} has no configured status name for {status}")
        await self.move_ticket(key, name)

    @abstractmethod
    async def move_ticket(self, key: str, status_name: str) -> bool: ...

    @abstractmethod
    async def aclose(self) -> None: ...

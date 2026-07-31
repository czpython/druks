from abc import ABC, abstractmethod

from .enums import TicketStatus


class Tracker(ABC):
    # Transport and API errors a caller should expect and handle, so consumers
    # can `except tracker.known_exceptions` without importing provider types.
    known_exceptions = ()

    async def __aenter__(self) -> "Tracker":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

    @abstractmethod
    async def set_status(self, key: str, status: TicketStatus) -> None: ...

    @abstractmethod
    async def aclose(self) -> None: ...

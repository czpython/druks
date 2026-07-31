from abc import ABC, abstractmethod
from typing import ClassVar, Self

from .enums import TicketStatus


class Tracker(ABC):
    source: ClassVar[str]

    # Transport and API errors a caller should expect and handle, so consumers
    # can `except tracker.known_exceptions` without importing provider types.
    known_exceptions: ClassVar[tuple[type[BaseException], ...]] = ()

    # Set by each provider's __init__ from its (provider-specific) settings.
    _status_names: dict[TicketStatus, str]

    @classmethod
    @abstractmethod
    def from_settings(cls, *, ready_for_agent_status: str = "") -> Self:
        """Build a configured instance from global settings, reading this
        provider's own credentials. ``ready_for_agent_status`` is the operator's
        name for the READY_FOR_AGENT status (empty leaves it unmapped) — the one
        status name the caller supplies. Raise ``TrackerNotConfigured`` if the
        credentials are absent. Registered by ``source`` in ``helpers``."""

    async def __aenter__(self) -> "Tracker":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

    @abstractmethod
    async def set_status(self, key: str, status: TicketStatus) -> None: ...

    @abstractmethod
    async def aclose(self) -> None: ...

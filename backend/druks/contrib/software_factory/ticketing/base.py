from abc import ABC, abstractmethod
from collections.abc import Collection
from typing import Self

from druks.accounts import Account

from .enums import TicketStatus


class Tracker(ABC):
    # The errors a caller handles, so it can catch them without importing provider types.
    known_exceptions = ()
    # The MCP grant issuer whose subjects are this tracker's user ids.
    authority: str
    # The configured name of each mapped status. An unmapped status is absent.
    _status_names: dict[TicketStatus, str]

    async def get_account_id(self, user_id: str | None) -> str | None:
        """The one Druks account that connected this tracker as the user."""
        if user_id and (account := await Account.lookup(self.authority, user_id)):
            return account.id

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

    def _find_status(self, names: Collection[str]) -> TicketStatus | None:
        return next((status for status, name in self._status_names.items() if name in names), None)

    async def list_status_choices(self) -> list[dict[str, str]]:
        """The statuses an operator can pick, in the shape a ``Choices`` source returns."""
        return []

    @abstractmethod
    async def get_status(self, key: str) -> TicketStatus | None:
        """The mapped status the ticket is at. None when it is at an unmapped one."""

    @abstractmethod
    async def set_status(self, key: str, status: TicketStatus) -> None: ...

    async def release(self, key: str) -> None:
        """Put a ticket the build still holds at the resting status.

        A ticket a person moved stays where it is.
        """
        if await self.get_status(key) in (TicketStatus.IN_PROGRESS, TicketStatus.IN_REVIEW):
            await self.set_status(key, TicketStatus.BACKLOG)

    @abstractmethod
    async def aclose(self) -> None: ...

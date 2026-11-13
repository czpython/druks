from abc import ABC, abstractmethod
from typing import Self

from druks.accounts import Account

from .enums import TicketStatus


class Tracker(ABC):
    # The errors a caller handles, so it can catch them without importing provider types.
    known_exceptions = ()
    # The MCP grant issuer whose subjects are this tracker's user ids.
    authority: str

    def __init__(self, *, status_names: dict[TicketStatus, str]) -> None:
        # An empty name leaves that status unmapped.
        self._status_names = {status: name for status, name in status_names.items() if name}

    async def get_account_id(self, user_id: str | None) -> str | None:
        """The one Druks account that connected this tracker as the user."""
        if user_id and (account := await Account.lookup(self.authority, user_id)):
            return account.id

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

    async def list_status_choices(self) -> list[dict[str, str]]:
        """The statuses an operator can pick, in the shape a ``Choices`` source returns."""
        return []

    @abstractmethod
    async def get_ticket(self, key: str) -> dict:
        """The ticket in the shape of a ``ticket.transitioned`` payload, with no actor."""

    @abstractmethod
    async def get_status_name(self, key: str) -> str | None:
        """The tracker's own name for the status the ticket is at. None when it has none."""

    @abstractmethod
    async def set_status(self, key: str, status: TicketStatus) -> None: ...

    async def get_blocked_by(self, key: str) -> dict[str, bool]:
        """The key of each ticket that blocks this one, and whether it is open."""
        return {}

    async def get_blocking(self, key: str) -> list[dict]:
        """The tickets that this one blocks, each in the shape of ``get_ticket``."""
        return []

    async def release(self, key: str) -> None:
        """Put a ticket the build still holds at the resting status.

        A ticket a person moved stays where it is.
        """
        held = {
            name
            for status, name in self._status_names.items()
            if status in (TicketStatus.IN_PROGRESS, TicketStatus.IN_REVIEW)
        }
        if await self.get_status_name(key) in held:
            await self.set_status(key, TicketStatus.BACKLOG)

    @abstractmethod
    async def aclose(self) -> None: ...

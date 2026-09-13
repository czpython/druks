from abc import ABC, abstractmethod

from druks.accounts.models import Account

from .enums import TicketStatus


class Tracker(ABC):
    # Transport and API errors a caller should expect and handle, so consumers
    # can `except tracker.known_exceptions` without importing provider types.
    known_exceptions = ()
    # The MCP grant issuer whose subjects are this tracker's user ids.
    authority: str

    async def get_account_id(self, user_id: str) -> str | None:
        """The one Druks account that connected this tracker as the user."""
        if account := await Account.lookup(self.authority, user_id):
            return account.id

    async def __aenter__(self) -> "Tracker":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

    @abstractmethod
    async def set_status(self, key: str, status: TicketStatus) -> None: ...

    @abstractmethod
    async def aclose(self) -> None: ...

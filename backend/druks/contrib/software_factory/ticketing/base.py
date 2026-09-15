from abc import ABC, abstractmethod
from typing import Self

from druks.accounts.models import Account
from druks.db import db_session

from .enums import TicketStatus


class Tracker(ABC):
    # The errors a caller handles, so it can catch them without importing provider types.
    known_exceptions = ()
    # The MCP grant issuer whose subjects are this tracker's user ids.
    authority: str

    async def get_account_id(self, user_id: str) -> str | None:
        """The one Druks account that connected this tracker as the user."""
        if account := await Account.lookup(db_session(), self.authority, user_id):
            return account.id

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

    async def list_status_choices(self) -> list[dict[str, str]]:
        """The statuses an operator can pick, in the shape a ``Choices`` source returns."""
        return []

    @abstractmethod
    async def set_status(self, key: str, status: TicketStatus) -> None: ...

    @abstractmethod
    async def aclose(self) -> None: ...

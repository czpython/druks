import copyreg
from typing import Any


class DruksError(Exception):
    """Base of every druks exception. Pickle rebuilds one from its args and
    attributes without a constructor call, so a DBOS step replays the error
    it recorded."""

    def __reduce__(self) -> tuple[Any, ...]:
        return copyreg.__newobj__, (type(self),), {"args": self.args, **vars(self)}


class DetachedRowError(DruksError):
    """A row was used outside the session that loaded it."""

    def __init__(self, model: str) -> None:
        super().__init__(f"A {model} row is not loaded in a session. Read it where it is used.")


class LockHeldError(DruksError):
    """``lock(blocking=False)`` found another holder."""

    def __init__(self, name: str) -> None:
        super().__init__(f"Lock {name!r} has a holder. Try again after the holder releases it.")


class LockLostError(DruksError):
    """The holder lost the lock before its block ended."""

    def __init__(self, name: str) -> None:
        super().__init__(f"Lock {name!r} left its holder before the work ended. Run it again.")


class SessionNotBoundError(DruksError):
    """``db_session()`` ran on a task that holds no session."""

    def __init__(self) -> None:
        super().__init__(
            "No database session is bound to this task. "
            "Read inside a @step, a dispatch(), or a request."
        )

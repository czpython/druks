import copyreg
from typing import Any


class DruksError(Exception):
    """Base of every druks exception. Pickle rebuilds one from its args and
    attributes without a constructor call, so a DBOS step replays the error
    it recorded."""

    def __reduce__(self) -> tuple[Any, ...]:
        return copyreg.__newobj__, (type(self),), {"args": self.args, **vars(self)}

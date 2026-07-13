from .base import Harness
from .exceptions import HarnessError


def get_harnesses() -> tuple[type[Harness], ...]:
    """The registry: every ``Harness`` subclass, sorted by name for a stable
    order. The harness modules are imported in this package's ``__init__`` so
    the subclasses are enrolled."""
    return tuple(sorted(Harness.__subclasses__(), key=lambda harness: harness.name))


def get_harness_for_model(model: str) -> type[Harness]:
    """The harness that runs ``model``, matched by name namespace — a model in a
    known namespace routes even if it postdates this release. A miss means no
    installed harness owns its namespace."""
    for harness in get_harnesses():
        if harness.has_model(model):
            return harness
    raise HarnessError(f"no harness runs model {model!r}")

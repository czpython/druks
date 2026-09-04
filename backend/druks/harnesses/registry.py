from .base import Harness


def get_harnesses() -> tuple[type[Harness], ...]:
    """The registry: every ``Harness`` subclass, sorted by name for a stable
    order. The harness modules are imported in this package's ``__init__`` so
    the subclasses are enrolled."""
    return tuple(sorted(Harness.__subclasses__(), key=lambda harness: harness.name))


def get_harness(name: str) -> type[Harness] | None:
    """The harness registered under ``name``, or None."""
    for harness in get_harnesses():
        if harness.name == name:
            return harness

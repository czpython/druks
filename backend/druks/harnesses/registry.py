from .base import Harness
from .exceptions import UnknownModelError
from .providers import get_provider


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


def get_harness_for_model(model: str) -> type[Harness]:
    """The first registered harness whose providers include the one ``model``
    names; a miss raises."""
    try:
        provider = get_provider(model.partition("/")[0])
    except KeyError as exc:
        raise UnknownModelError(
            f"No installed harness runs model {model!r}; a model id is 'provider/model'."
        ) from exc
    for harness in get_harnesses():
        if harness.has_provider(provider):
            return harness
    raise UnknownModelError(f"No installed harness runs model {model!r}.")

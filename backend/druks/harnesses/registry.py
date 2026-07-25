from druks.user_settings.models import HarnessSettings

from .base import Harness
from .exceptions import HarnessError


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
    """The harness that runs ``model`` from its fetched-or-shipped model list.

    A miss raises loudly; namespace-shaped models do not route until a fetch
    stores them on the harness settings row.
    """
    for row in HarnessSettings.all():
        if model == row.name or any(m["id"] == model for m in row.allowed_models):
            return row.harness
    raise HarnessError(f"no harness runs model {model!r}")

from collections.abc import Awaitable, Callable
from typing import Any

from blinker import signal

__all__ = ["subscribe"]

Subscriber = Callable[..., Awaitable[None]]


def subscribe(name: str, **filters: Any) -> Callable[[Subscriber], Subscriber]:
    """Register an async subscriber for the named signal.

    ``filters`` are equality matches against the published kwargs; ``__``
    descends into dicts (``subject__type="work_item"``); a ``Gate`` class
    stands for its ``name``. A non-matching publication skips the
    subscriber, so the body starts at the real work."""

    def register(fn: Subscriber) -> Subscriber:
        # Lazy import: druks.workflows imports this module.
        from druks.workflows import Gate

        matches = {}
        for lookup, expected in filters.items():
            if isinstance(expected, type) and issubclass(expected, Gate):
                expected = expected.name
            matches[lookup] = expected

        async def receiver(_sender: Any, **kwargs: Any) -> None:
            for lookup, expected in matches.items():
                value: Any = kwargs
                for part in lookup.split("__"):
                    value = value.get(part) if isinstance(value, dict) else None
                if value != expected:
                    return
            await fn(**kwargs)

        # weak=False: ``receiver`` is a local closure nothing else references, so a
        # weak ref would drop it the moment registration returns.
        signal(name).connect(receiver, weak=False)
        return fn

    return register


async def publish(name: str, **kwargs: Any) -> None:
    """Fire the named signal — each subscriber gets ``kwargs``, awaited in turn.

    A subscriber exception propagates to the publisher: a webhook responds 5xx
    and the provider redelivers; the durable publish steps (run lifecycle,
    ``set_state`` fan-out) enable DBOS retries. Delivery is therefore
    at-least-once — subscribers must be idempotent."""
    await signal(name).send_async(None, **kwargs)

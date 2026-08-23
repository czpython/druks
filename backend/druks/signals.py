import inspect
from collections.abc import Awaitable, Callable
from typing import Any

from blinker import signal

from druks.apps.exceptions import SubscriberDeclarationError

__all__ = ["subscribe"]

Subscriber = Callable[..., Awaitable[None]]

# Published so filters can match them, never handed to a body: the run row and its
# durable kind are the substrate's, and a subscriber reacts to the subject.
_ROUTING = frozenset({"run", "kind"})


def subscribe(name: str, **filters: Any) -> Callable[[Subscriber], Subscriber]:
    """Register an async subscriber for the named signal.

    ``filters`` are equality matches against the published kwargs; ``__``
    descends into dicts (``payload__terminal=True``); a ``Gate`` class stands for
    its ``name``. ``workflow=Build`` narrows to that workflow and to the subject
    that workflow declares, handing the body that subject; ``subject=WorkItem``
    on its own narrows to any workflow about one. A non-matching publication
    skips the subscriber, so the body starts at the real work."""

    def register(fn: Subscriber) -> Subscriber:
        # Lazy import: druks.workflows imports this module.
        from druks.durable.datastructures import Subject
        from druks.models import StoredSubject
        from druks.workflows import Gate

        if asked_for_routing := sorted(_ROUTING & set(inspect.signature(fn).parameters)):
            raise SubscriberDeclarationError(
                f"{fn.__name__}() asks for {asked_for_routing} — filters match on those, "
                "bodies never receive them. Narrow with workflow=YourWorkflow and take "
                "the facts you react to."
            )
        workflow_class = filters.pop("workflow", None)
        subject_class = filters.pop("subject", None)
        if workflow_class and subject_class:
            raise SubscriberDeclarationError(
                f"{fn.__name__}() names both workflow= and subject= — "
                f"{workflow_class.__name__} already declares what its runs are about."
            )
        if workflow_class:
            filters["kind"] = workflow_class.kind
            subject_class = workflow_class.subject
        if subject_class:
            if not (
                isinstance(subject_class, type)
                and issubclass(subject_class, Subject | StoredSubject)
            ):
                raise SubscriberDeclarationError(
                    f"{fn.__name__}() filters on subject={subject_class!r}, which is not a "
                    "Subject or StoredSubject subclass."
                )
            filters["subject__type"] = subject_class.subject_type
        matches = {}
        for lookup, expected in filters.items():
            if isinstance(expected, type) and issubclass(expected, Gate):
                expected = expected.name
            matches[lookup] = expected

        async def receiver(_sender: Any, **published: Any) -> None:
            for lookup, expected in matches.items():
                value: Any = published
                for part in lookup.split("__"):
                    value = value.get(part) if isinstance(value, dict) else None
                if value != expected:
                    return
            delivered = {key: value for key, value in published.items() if key not in _ROUTING}
            if subject_class:
                subject = subject_class.get_for_subject_id(str(published["subject"]["id"]))
                if subject is None:
                    return
                delivered["subject"] = subject
            await fn(**delivered)

        # weak=False: ``receiver`` is a local closure nothing else references, so a
        # weak ref would drop it the moment registration returns.
        signal(name).connect(receiver, weak=False)
        return fn

    return register


async def publish(name: str, **kwargs: Any) -> None:
    """Fire the named signal — each subscriber gets ``kwargs``, awaited in turn.

    A subscriber exception propagates to the publisher: a webhook responds 5xx
    and the provider redelivers; the durable publish steps (run lifecycle,
    ``announce`` fan-out) enable DBOS retries. Delivery is therefore
    at-least-once — subscribers must be idempotent."""
    await signal(name).send_async(None, **kwargs)

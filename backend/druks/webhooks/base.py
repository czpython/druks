import json
import logging
from typing import Any

from fastapi import HTTPException, Request, status
from fastapi.responses import Response
from starlette.routing import compile_path

from druks.apps.registry import webhooks
from druks.settings import Settings

from .deliveries import mark_delivery, release_delivery

logger = logging.getLogger(__name__)


class Webhook:
    """Base class for one provider/category integration.

    Subclasses set ``provider`` and ``category`` (or override ``path``),
    then implement ``request_is_authentic``, ``get_action``, and one
    ``on_<action>`` method per action they handle. The base class owns
    body reading, authentication dispatch, deduplication, and routing
    to the right ``on_<action>`` method.

    Mark a class as ``abstract = True`` to opt out of auto-registration
    (e.g. when defining an intermediate base that other subclasses
    inherit from).
    """

    abstract: bool = False
    provider: str | None = None
    category: str | None = None
    path: str | None = None
    # Populated by __init_subclass__ for concrete subclasses.
    pattern: Any = None

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)

        # If this class didn't set ``abstract`` explicitly, reset to
        # False so abstractness doesn't cascade from a parent. Checking
        # ``cls.__dict__`` (not the MRO) is the only way to tell "the
        # subclass declared this" from "we inherited it" — without this
        # distinction, a grandchild of an abstract base would re-inherit
        # ``abstract = True`` and silently never register.
        if "abstract" not in cls.__dict__:
            cls.abstract = False

        if cls.abstract:
            return

        if not cls.path:
            if not cls.provider or not cls.category:
                raise TypeError(
                    f"{cls.__name__}: concrete Webhook subclass must set either "
                    "`path` or both `provider` and `category`.",
                )
            cls.path = f"{cls.provider}/{cls.category}/"

        # Starlette's compile_path anchors to ``^/...$``. We store the
        # convention path WITHOUT a leading slash (``"github/events/"``)
        # so it composes cleanly with the catch-all ``{hook_path:path}``
        # FastAPI route — but we compile with a leading slash so the
        # regex matches the same shape the router hands us.
        cls.pattern, _, _ = compile_path("/" + cls.path)
        webhooks.register(cls)

    def __init__(self, request: Request, kwargs: dict[str, Any], settings: Settings) -> None:
        self.request = request
        self.kwargs = kwargs
        self.settings = settings
        self.raw_body: bytes = b""
        self._data_cached: Any = _UNSET

    @property
    def data(self) -> Any:
        if self._data_cached is _UNSET:
            self._data_cached = self.get_data()
        return self._data_cached

    def get_data(self) -> Any:
        """Parse the request body. Default: JSON decode the raw bytes.

        Override for form-encoded payloads, signed envelopes, etc.
        """
        return json.loads(self.raw_body)

    def get_action(self) -> str:
        """Return a token that selects which ``on_<action>`` runs.

        The framework calls ``getattr(self, f"on_{action}")``. Return any
        string that's also a valid Python identifier — including
        underscores — and define a matching method.
        """
        raise NotImplementedError

    async def request_is_authentic(self) -> bool:
        """Verify the request is from the claimed provider.

        Return ``True`` if the request should be processed, ``False`` to
        reject with a generic 401. Subclasses may also raise
        ``HTTPException`` directly for richer responses (e.g. 403
        "Repository not allowed").
        """
        raise NotImplementedError

    def delivery_key(self) -> str | None:
        """Return a stable key for at-most-once delivery, or None to skip.

        Default disables dedup. Subclasses return e.g. the
        ``x-github-delivery`` header or a payload-derived hash; storage
        is the base class's job (``deliveries.mark_delivery``).
        """
        return None

    async def on_duplicate(self) -> Response:
        """Response for a re-delivery of a key we've already stored.

        Default: 200 with a small JSON body — providers see success and
        won't retry, but the operator can grep logs to spot replays.
        """
        return Response(
            content=b'{"accepted":false,"duplicate":true}',
            media_type="application/json",
        )

    async def on_unhandled(self, action: str) -> Response:
        """Fallback when no ``on_<action>`` method matches.

        Default returns 200 so unknown event types from the provider
        don't trigger their retry loop. Override to log or raise.
        """
        return Response(
            content=b'{"accepted":true,"handled":false}',
            media_type="application/json",
        )

    def log_ignored(self, *, event: str, reason: str, **extra: Any) -> None:
        # Operators grep ``webhook_ignored_reason=`` to find events that
        # arrived but didn't fire a handler. Keep the message shape stable.
        fields = {"webhook_event": event, "webhook_ignored_reason": reason, **extra}
        logger.info("webhook ignored: %s (%s)", reason, event, extra=fields)

    async def respond(self) -> Response:
        self.raw_body = await self.request.body()

        if not await self.request_is_authentic():
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid webhook signature.",
            )

        provider = self.provider or self.path or "unknown"
        key = self.delivery_key()
        if not await mark_delivery(provider, key):
            return await self.on_duplicate()

        # The claim is held for the attempt so concurrent duplicates still short-circuit;
        # if the handler fails, release it so the provider's retry re-processes instead of
        # being silently swallowed as a duplicate until the dedup TTL expires.
        try:
            action = self.get_action()
            handler = getattr(self, f"on_{action}", None)
            if not handler:
                return await self.on_unhandled(action)
            return await handler()
        except BaseException:
            await release_delivery(provider, key)
            raise


class _Unset:
    def __repr__(self) -> str:
        return "<UNSET>"


_UNSET = _Unset()

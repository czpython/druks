import contextlib
import functools
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import httpx

from druks.models import snake_name
from druks.redis import get_client
from druks.sandbox.datastructures import SandboxSecret
from druks.settings import load_settings

from .constants import OAUTH_TOKEN_TTL_SKEW_SECONDS
from .exceptions import ResourceError

if TYPE_CHECKING:
    from .base import Service

STEPS = ("create", "update", "delete", "get_token")


def _reuse_token(get_token: Callable) -> Callable:
    @functools.wraps(get_token)
    async def get_kept_token(resource: "Resource") -> tuple[str, datetime | None]:
        redis = get_client()
        key = f"{resource.service.slug}:resource_token:{resource.path}"
        if value := await redis.get(key):
            return value.decode(), datetime.now(UTC) + timedelta(seconds=await redis.ttl(key))
        value, expires_at = await get_token(resource)
        if expires_at:
            lifetime = (expires_at - datetime.now(UTC)).total_seconds()
            if (ttl := int(lifetime) - OAUTH_TOKEN_TTL_SKEW_SECONDS) > 0:
                await redis.set(key, value, ex=ttl)
        return value, expires_at

    return get_kept_token


class Resource:
    """An item that an app keeps at a provider, for example a Cloudflare Worker. Its class
    does only what the token of the resource cannot do: ``create``, ``update``,
    ``delete``, and ``get_token``."""

    service: type["Service"]
    slug: str
    key: str

    def __new__(cls, key: str) -> "Resource":
        if cls.service.is_managed():
            cls = _managed(cls)
        resource = object.__new__(cls)
        resource.key = key
        return resource

    def __init__(self, name: str) -> None:
        entry = load_settings().services.get(self.service.slug, {})
        self.name = entry.get("resource_prefix", "") + name

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        for name, value in cls.__dict__.items():
            if callable(value) and not name.startswith("_") and name not in STEPS:
                raise TypeError(
                    f"{cls.__name__}.{name} is not a step. A resource has only the steps "
                    f"{', '.join(STEPS)}. Call the provider with get_token() instead."
                )
        if "slug" not in cls.__dict__:
            cls.slug = snake_name(cls.__name__)
        if "get_token" in cls.__dict__:
            cls.get_token = _reuse_token(cls.__dict__["get_token"])

    @property
    def path(self) -> str:
        return f"{self.slug}/{self.name}"

    async def get_token(self) -> tuple[str, datetime | None]:
        secrets = (await self.service.get()).secrets
        if len(secrets) == 1:
            [value] = secrets.values()
            return value, None
        raise TypeError(
            f"{self.service.slug} has {len(secrets)} secrets, so {type(self).__name__} has no "
            "default token. Override get_token() to choose one."
        )

    async def get_secrets(self) -> list[SandboxSecret]:
        return [
            SandboxSecret(
                name=f"{self.service.slug}_api_token",
                secret_id=(await self.service.get()).id,
                resource=f"{self.slug}/{self.key}",
                host=self.service.host,
            )
        ]


class ManagedResource(Resource):
    """The four methods of a resource whose service a manager manages. Each method calls
    the ``url`` of the card with the manager token."""

    async def create(self) -> None:
        await self._send("create", "PUT", self.path)

    async def update(self, **attrs: Any) -> Any:
        [(attr, value)] = attrs.items()
        if value is None:
            await self._send("update", "DELETE", f"{self.path}/{attr}")
            return
        return (await self._send("update", "PUT", f"{self.path}/{attr}", {attr: value})).json()

    async def delete(self) -> None:
        await self._send("delete", "DELETE", self.path, missing_ok=True)

    async def get_token(self) -> tuple[str, datetime | None]:
        answer = (await self._send("get_token", "POST", f"{self.path}/token")).json()
        return answer["value"], datetime.fromisoformat(answer["expires_at"])

    async def _send(
        self,
        step: str,
        method: str,
        path: str,
        body: dict | None = None,
        *,
        missing_ok: bool = False,
    ) -> httpx.Response:
        card = await self.service.get()
        token = load_settings().manager.token.get_secret_value()
        transport = httpx.AsyncHTTPTransport(retries=2)
        try:
            async with httpx.AsyncClient(transport=transport, timeout=30.0) as client:
                response = await client.request(
                    method,
                    f"{card.identity['url'].rstrip('/')}/{path}",
                    json=body,
                    headers={"Authorization": f"Bearer {token}"},
                )
            if not (missing_ok and response.status_code == 404):
                response.raise_for_status()
        except httpx.HTTPStatusError as error:
            reason = ""
            with contextlib.suppress(ValueError, KeyError, TypeError):
                reason = str(error.response.json()["message"])
            raise ResourceError(
                step, self.path, reason, status_code=error.response.status_code
            ) from error
        except httpx.TransportError as error:
            raise ResourceError(step, self.path, str(error) or type(error).__name__) from error
        return response


@functools.cache
def _managed(resource: type[Resource]) -> type[Resource]:
    # Without this slug, the subclass gets ``managed_workers`` from its own name.
    return type(f"Managed{resource.__name__}", (ManagedResource, resource), {"slug": resource.slug})

from datetime import UTC, datetime

from druks.redis import get_client

from .constants import DELIVERY_LAST_PREFIX, DELIVERY_SEEN_PREFIX

_DEDUP_TTL_SECONDS = 24 * 60 * 60


async def mark_delivery(provider: str, key: str | None) -> bool:
    """A duplicate still bumps freshness — a replay proves the pipe works."""
    client = get_client()
    await client.set(f"{DELIVERY_LAST_PREFIX}{provider}", datetime.now(UTC).isoformat())
    if key is None:
        return True
    return bool(
        await client.set(
            f"{DELIVERY_SEEN_PREFIX}{provider}:{key}", "1", nx=True, ex=_DEDUP_TTL_SECONDS
        )
    )


async def release_delivery(provider: str, key: str | None) -> None:
    # Undo the dedup claim when the handler failed, so the provider's retry re-processes
    # instead of hitting on_duplicate for the whole dedup TTL. Freshness stays bumped —
    # the delivery did arrive.
    if key is None:
        return
    await get_client().delete(f"{DELIVERY_SEEN_PREFIX}{provider}:{key}")


async def last_delivery_at(provider: str) -> datetime | None:
    raw = await get_client().get(f"{DELIVERY_LAST_PREFIX}{provider}")
    return datetime.fromisoformat(raw.decode()) if raw else None

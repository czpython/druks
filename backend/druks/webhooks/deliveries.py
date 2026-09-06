from druks.redis import get_client

from .constants import DELIVERY_SEEN_PREFIX

_DEDUP_TTL_SECONDS = 24 * 60 * 60


async def mark_delivery(provider: str, key: str | None) -> bool:
    if key is None:
        return True
    return bool(
        await get_client().set(
            f"{DELIVERY_SEEN_PREFIX}{provider}:{key}", "1", nx=True, ex=_DEDUP_TTL_SECONDS
        )
    )


async def release_delivery(provider: str, key: str | None) -> None:
    # Undo the dedup claim when the handler failed, so the provider's retry re-processes
    # instead of hitting on_duplicate for the whole dedup TTL.
    if key is None:
        return
    await get_client().delete(f"{DELIVERY_SEEN_PREFIX}{provider}:{key}")

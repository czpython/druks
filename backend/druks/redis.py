import redis.asyncio as aioredis

from druks.settings import load_settings

_client: aioredis.Redis | None = None


def get_client() -> aioredis.Redis:
    global _client
    if not _client:
        _client = aioredis.from_url(load_settings().redis_url.get_secret_value())
    return _client


async def close_client() -> None:
    global _client
    if _client:
        await _client.aclose()
        _client = None

import json
import logging

import httpx

from druks.redis import get_client

from . import exceptions
from .constants import DIRECTORY_CACHE_KEY, DIRECTORY_CACHE_TTL_SECONDS
from .models import ProviderCatalog, ProviderKey
from .providers import error_tag, is_registered

logger = logging.getLogger(__name__)

_MODELS_DEV_URL = "https://models.dev/api.json"
_TIMEOUT_SECONDS = 20.0


async def list_providers() -> list[dict]:
    """Every models.dev provider that runs on one API key, ``{"provider",
    "label", "models"}`` each. Held in Redis for a day."""
    redis = get_client()
    cached = await redis.get(DIRECTORY_CACHE_KEY)
    if cached:
        return json.loads(cached)
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
            response = await client.get(_MODELS_DEV_URL)
    except httpx.TimeoutException as exc:
        raise exceptions.CatalogError("timeout") from exc
    except httpx.HTTPError as exc:
        raise exceptions.CatalogError("network") from exc
    if response.status_code != 200:
        raise exceptions.CatalogError(error_tag(response.status_code))
    providers = parse_providers(response.text)
    await redis.set(DIRECTORY_CACHE_KEY, json.dumps(providers), ex=DIRECTORY_CACHE_TTL_SECONDS)
    return providers


def parse_providers(raw: str) -> list[dict]:
    """The models.dev providers a key can reach: the key form holds one
    environment variable, so a provider that needs more has no place in it."""
    try:
        providers = []
        for provider_id, provider in json.loads(raw).items():
            if len(provider.get("env") or []) != 1:
                continue
            models = [
                {"id": f"{provider_id}/{model_id}", "label": model.get("name") or model_id}
                for model_id, model in provider.get("models", {}).items()
            ]
            if models:
                providers.append(
                    {
                        "provider": provider_id,
                        "label": provider.get("name") or provider_id,
                        "models": sorted(models, key=lambda model: model["label"]),
                    }
                )
    except json.JSONDecodeError as exc:
        raise exceptions.CatalogError("unparseable") from exc
    except (AttributeError, TypeError) as exc:
        raise exceptions.CatalogError("unexpected_payload") from exc
    if providers:
        return sorted(providers, key=lambda provider: provider["label"])
    raise exceptions.CatalogError("empty_list")


async def add_provider(provider_id: str) -> ProviderCatalog:
    """Make a directory provider one of the installation's, with the model
    list it publishes. A provider the directory does not list raises ``KeyError``."""
    for provider in await list_providers():
        if provider["provider"] == provider_id:
            return await ProviderCatalog.create(
                provider_id, provider["models"], label=provider["label"]
            )
    raise KeyError(provider_id)


async def refresh_added_catalogs() -> None:
    """Re-read the directory for every provider the operator added by key."""
    keys = await ProviderKey.list_all()
    added = [row.provider for row in keys if not is_registered(row.provider)]
    for provider_id in added:
        try:
            await add_provider(provider_id)
        except (exceptions.CatalogError, KeyError) as exc:
            logger.warning("directory refresh of %s failed: %s", provider_id, exc)

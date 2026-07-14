import json
import re
from urllib.parse import urlparse

import httpx

from druks.mcp.constants import (
    NAME_PATTERN,
    REGISTRY_CACHE_TTL_SECONDS,
    REGISTRY_SEARCH_CACHE_PREFIX,
    REGISTRY_SEARCH_URL,
)
from druks.mcp.exceptions import RegistryUnavailableError
from druks.mcp.oauth import _http
from druks.redis import get_client

# Every entry in the registry is an MCP server, so these words carry no
# information in a druks-side name: io.github.grafana/mcp-grafana and
# io.github.getsentry/sentry-mcp both name their product plainly.
_NOISE_TOKENS = frozenset({"mcp", "server"})


def derive_server_name(registry_name: str) -> str:
    """The druks-side name for a registry entry — one identifier reused as the
    MCP config key and env-var stem (see NAME_PATTERN):
    ``io.github.grafana/mcp-grafana`` -> ``grafana``. A digit-led remainder
    gets an ``mcp_`` prefix so the name stays letter-led."""
    segment = registry_name.partition("/")[2] or registry_name
    tokens = [token for token in re.split(r"[^a-z0-9]+", segment.lower()) if token]
    while len(tokens) > 1 and tokens[0] in _NOISE_TOKENS:
        tokens = tokens[1:]
    while len(tokens) > 1 and tokens[-1] in _NOISE_TOKENS:
        tokens = tokens[:-1]
    name = "_".join(tokens)
    if not NAME_PATTERN.match(name):
        name = f"mcp_{name}"
    return name


async def search_registry(query: str) -> list[dict]:
    """The latest version of every server matching ``query``, verbatim from
    the official registry, briefly cached in Redis. Raises
    RegistryUnavailableError on any failure — an unreachable registry must
    never read as an empty result."""
    redis = get_client()
    cache_key = f"{REGISTRY_SEARCH_CACHE_PREFIX}{query}"

    if cached := await redis.get(cache_key):
        return json.loads(cached)
    try:
        async with _http() as client:
            response = await client.get(
                REGISTRY_SEARCH_URL, params={"search": query, "version": "latest"}
            )
            response.raise_for_status()
        entries = response.json()["servers"]
    except httpx.HTTPError as error:
        raise RegistryUnavailableError(query, str(error)) from error
    except (ValueError, KeyError, TypeError) as error:
        raise RegistryUnavailableError(query, "malformed registry response") from error
    await redis.set(cache_key, json.dumps(entries), ex=REGISTRY_CACHE_TTL_SECONDS)
    return entries


def resolve_candidates(entries: list[dict], pins: dict[str, str]) -> dict[str, dict]:
    """One installable candidate per entry reachable over streamable HTTP —
    from its own declared remote, or from a pinned official url the registry
    omits; stdio/oci-only entries drop out. Keyed by registry name, official
    candidates first. ``official`` is the trust badge: the publisher provably
    owns the remote's domain, or a pin vouches."""
    # A url pin lifts exactly one entry among those deriving its name. The pin
    # itself supplies the url, so this choice only picks display text; the
    # tiebreak just needs determinism: prefer the publisher whose own name
    # carries the pin key (getsentry for "sentry", against an aggregator's
    # com.mcparmory/sentry), then the first registry name.
    overrides: dict[str, str] = {}
    for key, value in pins.items():
        if not value.startswith("http"):
            continue
        names = sorted(
            (
                entry["server"]["name"]
                for entry in entries
                if derive_server_name(entry["server"]["name"]) == key
            ),
            key=lambda name: (key not in name.partition("/")[0].rsplit(".", 1)[-1], name),
        )
        if names:
            overrides[names[0]] = value

    candidates = []
    for entry in entries:
        server = entry["server"]
        registry_name = server["name"]
        remote = next(
            (
                remote
                for remote in server.get("remotes") or []
                if remote.get("type") == "streamable-http"
            ),
            {},
        )
        url = overrides.get(registry_name) or remote.get("url", "")
        if not url:
            continue
        # The registry verifies namespace ownership (DNS or GitHub), so a
        # remote living on the reversed namespace (com.grafana ->
        # *.grafana.com) is provably the publisher's own endpoint. io.github.*
        # reverses to *.github.io, where real remotes never sit — those
        # publishers need a pin.
        namespace = registry_name.partition("/")[0]
        domain = ".".join(reversed(namespace.split(".")))
        host = urlparse(url).hostname or ""
        derived = derive_server_name(registry_name)
        official = (
            registry_name in overrides  # a url pinned in druks' own repo
            or pins.get(derived) == namespace
            or host == domain
            or host.endswith(f".{domain}")
        )
        candidates.append(
            {
                "name": derived,
                "registry_name": registry_name,
                "description": server["description"],
                "url": url,
                "official": official,
                "headers": remote.get("headers") or [],
            }
        )
    candidates.sort(key=lambda candidate: (not candidate["official"], candidate["name"]))
    return {candidate["registry_name"]: candidate for candidate in candidates}

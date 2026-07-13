import json
import re
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx

from druks.mcp.constants import (
    NAME_PATTERN,
    REGISTRY_CACHE_TTL_SECONDS,
    REGISTRY_SEARCH_URL,
)
from druks.mcp.exceptions import InvalidTrustedPinsError, RegistryUnavailableError

# Every entry in the registry is an MCP server, so these words carry no
# information in a druks-side name: io.github.grafana/mcp-grafana and
# io.github.getsentry/sentry-mcp both name their product plainly.
_NOISE_TOKENS = frozenset({"mcp", "server"})

# One resolve is one GET; typing in the picker re-queries, the cache absorbs it.
_search_cache: dict[str, tuple[float, list[dict]]] = {}


def _http() -> httpx.AsyncClient:
    # One construction point so the suite can swap in a MockTransport client.
    return httpx.AsyncClient(timeout=15.0, follow_redirects=True)


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


def load_trusted_pins(path: Path) -> dict[str, str]:
    """The repo's trust pins, ``derived name -> value`` with the value
    disambiguated by content: ``http…`` is an official url the registry entry
    omits; anything else is a trusted publisher namespace. A file that can't
    be read or parsed fails loudly — a quietly empty pin set would silently
    downgrade every pinned server to community."""
    try:
        raw = json.loads(path.read_text())
    except OSError as error:
        raise InvalidTrustedPinsError(path, str(error)) from error
    except json.JSONDecodeError as error:
        raise InvalidTrustedPinsError(path, f"not valid JSON ({error})") from error
    if not isinstance(raw, dict) or not all(isinstance(value, str) for value in raw.values()):
        raise InvalidTrustedPinsError(path, "must be a JSON object of name → value strings")
    return raw


async def search_registry(query: str) -> list[dict]:
    """One GET against the official registry: the latest version of every
    server matching ``query``, verbatim. Raises RegistryUnavailableError on
    any failure — an unreachable registry must never read as an empty result."""
    now = time.monotonic()
    cached = _search_cache.get(query)
    if cached and cached[0] > now:
        return cached[1]
    try:
        async with _http() as client:
            response = await client.get(
                REGISTRY_SEARCH_URL, params={"search": query, "version": "latest"}
            )
            response.raise_for_status()
            payload = response.json()
    except httpx.HTTPError as error:
        raise RegistryUnavailableError(query, str(error)) from error
    except ValueError as error:
        raise RegistryUnavailableError(query, "malformed JSON in the response") from error
    entries = payload.get("servers") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        raise RegistryUnavailableError(query, "response carries no servers list")
    _search_cache[query] = (now + REGISTRY_CACHE_TTL_SECONDS, entries)
    return entries


def resolve_candidates(entries: list[dict], pins: dict[str, str]) -> list[dict]:
    """The installable view of a registry search: one candidate per entry
    reachable over streamable HTTP — from its own declared remote, or from a
    pinned official url the registry omits. stdio/oci-only entries drop out
    (druks runs no server processes). ``official`` is the trust badge: the
    publisher provably owns the remote's domain, or a pin vouches. Everything
    displayed — name, description — comes from the registry entry."""
    overrides = _url_pin_holders(entries, pins)
    candidates = []
    for entry in entries:
        server = entry["server"]
        registry_name = server["name"]
        remote = _streamable_http_remote(server)
        url = overrides.get(registry_name) or remote.get("url", "")
        if not url:
            continue
        namespace = registry_name.partition("/")[0]
        derived = derive_server_name(registry_name)
        official = (
            # A url pinned in druks' own repo is official by construction.
            registry_name in overrides
            or pins.get(derived, "") == namespace
            or _publisher_owns(namespace, url)
        )
        candidates.append(
            {
                "name": derived,
                "registry_name": registry_name,
                "description": server.get("description", ""),
                "url": url,
                "official": official,
                "headers": [_header_spec(header) for header in remote.get("headers") or []],
            }
        )
    candidates.sort(key=lambda candidate: (not candidate["official"], candidate["name"]))
    return candidates


def _streamable_http_remote(server: dict) -> dict:
    for remote in server.get("remotes") or []:
        if remote.get("type") == "streamable-http":
            return remote
    return {}


def _publisher_owns(namespace: str, url: str) -> bool:
    # The registry verifies namespace ownership (DNS or GitHub), so the
    # reversed namespace is a domain the publisher provably controls; the rule
    # is simply "the remote lives on it" (com.grafana -> *.grafana.com).
    # io.github.* reverses to *.github.io, where real remotes never sit —
    # those publishers need a pin.
    domain = ".".join(reversed(namespace.split(".")))
    host = urlparse(url).hostname or ""
    return host == domain or host.endswith(f".{domain}")


def _url_pin_holders(entries: list[dict], pins: dict[str, str]) -> dict[str, str]:
    # A url pin names one druks server; among entries deriving that name,
    # exactly one carries the override. The pin itself supplies the url — the
    # trust-critical part — so the choice only picks display text, and the
    # tiebreak just needs determinism: prefer the publisher whose own name
    # carries the pin key (getsentry for "sentry", against an aggregator's
    # com.mcparmory/sentry), then the first registry name lexicographically.
    holders: dict[str, str] = {}
    for key, value in pins.items():
        if not value.startswith("http"):
            continue
        names = [
            entry["server"]["name"]
            for entry in entries
            if derive_server_name(entry["server"]["name"]) == key
        ]
        if not names:
            continue
        names.sort(key=lambda name: (key not in name.partition("/")[0].rsplit(".", 1)[-1], name))
        holders[names[0]] = value
    return holders


def _header_spec(header: dict) -> dict:
    # The declared-input fields the install form renders; everything but the
    # name is optional in the registry's server schema.
    return {
        "name": header["name"],
        "description": header.get("description", ""),
        "placeholder": header.get("placeholder", ""),
        "is_required": bool(header.get("isRequired")),
        "is_secret": bool(header.get("isSecret")),
        "format": header.get("format", ""),
    }

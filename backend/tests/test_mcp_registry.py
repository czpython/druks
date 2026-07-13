import json

import httpx
import pytest
from druks.mcp import registry
from druks.mcp.constants import REGISTRY_SEARCH_CACHE_PREFIX
from druks.mcp.exceptions import RegistryUnavailableError
from druks.mcp.registry import derive_server_name, resolve_candidates, search_registry
from druks.redis import get_client
from druks.settings import PACKAGED_MCP_TRUSTED

# Canned latest-version entries, shaped like the live /v0/servers?search=…
# payload (verified against the registry 2026-07-13): grafana publishes a
# streamable-http remote with a declared header; sentry's official entry is
# npm-only (its hosted url exists only as a druks pin); an aggregator
# (mcparmory) republishes both as stdio-only packages.
_GRAFANA_HEADER = {
    "name": "X-Grafana-URL",
    "description": "URL of your Grafana Cloud instance",
    "placeholder": "https://<instance>.grafana.net",
}
_GRAFANA = {
    "server": {
        "name": "io.github.grafana/mcp-grafana",
        "description": "An MCP server giving access to Grafana dashboards, data and more.",
        "version": "v0.17.2",
        "packages": [{"registryType": "oci", "transport": {"type": "stdio"}}],
        "remotes": [
            {
                "type": "streamable-http",
                "url": "https://mcp.grafana.com/mcp",
                "headers": [_GRAFANA_HEADER],
            }
        ],
    },
    "_meta": {"io.modelcontextprotocol.registry/official": {"isLatest": True}},
}
_SENTRY = {
    "server": {
        "name": "io.github.getsentry/sentry-mcp",
        "description": "MCP server for Sentry - error monitoring for AI assistants",
        "version": "0.25.0",
        "packages": [{"registryType": "npm", "transport": {"type": "stdio"}}],
    },
    "_meta": {"io.modelcontextprotocol.registry/official": {"isLatest": True}},
}


def _entry(name, *, description="aggregated wrapper", remotes=None):
    return {
        "server": {
            "name": name,
            "description": description,
            "version": "1.0.0",
            "packages": [{"registryType": "pypi", "transport": {"type": "stdio"}}],
            **({"remotes": remotes} if remotes is not None else {}),
        },
        "_meta": {"io.modelcontextprotocol.registry/official": {"isLatest": True}},
    }


_PINS = {"grafana": "io.github.grafana", "sentry": "https://mcp.sentry.dev/mcp"}


# --- the trust badge: rule, publisher pin, url pin ------------------------


def test_publisher_pin_marks_grafana_official_with_the_registry_url():
    # grafana's remote lives on grafana.com but the publisher namespace is a
    # GitHub one — the rule can't derive ownership, the pin vouches. The url
    # stays the live registry value.
    candidates = resolve_candidates([_GRAFANA], _PINS)

    assert [c["name"] for c in candidates] == ["grafana"]
    grafana = candidates[0]
    assert grafana["official"] is True
    assert grafana["url"] == "https://mcp.grafana.com/mcp"
    assert grafana["registry_name"] == "io.github.grafana/mcp-grafana"
    assert grafana["description"].startswith("An MCP server")


def test_url_pin_lifts_sentry_whose_registry_entry_has_no_remote():
    # The registry omits sentry's hosted url; the pin supplies it. Display
    # text still comes from the registry entry.
    candidates = resolve_candidates([_SENTRY], _PINS)

    sentry = next(c for c in candidates if c["name"] == "sentry")
    assert sentry["official"] is True
    assert sentry["url"] == "https://mcp.sentry.dev/mcp"
    assert "error monitoring" in sentry["description"]
    assert sentry["headers"] == []


def test_url_pin_attaches_to_the_product_publisher_not_the_aggregator():
    # Both entries derive "sentry"; the pinned url must ride the publisher
    # whose own name carries it (getsentry), and the aggregator's stdio-only
    # twin stays dropped rather than turning into a second official "sentry".
    entries = [_entry("com.mcparmory/sentry"), _SENTRY]

    candidates = resolve_candidates(entries, _PINS)

    sentries = [c for c in candidates if c["name"] == "sentry"]
    assert len(sentries) == 1
    assert sentries[0]["registry_name"] == "io.github.getsentry/sentry-mcp"
    assert "error monitoring" in sentries[0]["description"]


def test_domain_ownership_rule_needs_no_pin():
    # com.cloudflare reverses to cloudflare.com; a remote on a subdomain of it
    # is provably the publisher's own endpoint — official, zero upkeep.
    entries = [
        _entry(
            "com.cloudflare/browser",
            remotes=[{"type": "streamable-http", "url": "https://browser.mcp.cloudflare.com/mcp"}],
        )
    ]

    candidates = resolve_candidates(entries, {})

    assert candidates[0]["official"] is True


def test_unpinned_unmatched_remote_is_community():
    entries = [
        _entry(
            "io.github.someone/grafana-tools",
            remotes=[{"type": "streamable-http", "url": "https://tools.example.com/mcp"}],
        )
    ]

    candidates = resolve_candidates(entries, _PINS)

    assert [c["official"] for c in candidates] == [False]


def test_stdio_only_entries_are_dropped():
    # No streamable-http remote and no url pin — not installable, not shown.
    candidates = resolve_candidates([_entry("com.mcparmory/grafana")], _PINS)

    assert candidates == []


def test_official_candidates_sort_first():
    community = _entry(
        "io.github.someone/aardvark",
        remotes=[{"type": "streamable-http", "url": "https://aardvark.example.com/mcp"}],
    )

    candidates = resolve_candidates([community, _GRAFANA], _PINS)

    assert [c["name"] for c in candidates] == ["grafana", "aardvark"]


def test_packaged_pins_resolve_grafana_and_sentry():
    # The shipped trusted.json, end to end: grafana by publisher pin (registry
    # url kept), sentry by url pin (registry entry has no remote).
    pins = json.loads(PACKAGED_MCP_TRUSTED.read_text())

    candidates = resolve_candidates([_GRAFANA, _SENTRY], pins)

    assert [(c["name"], c["official"]) for c in candidates] == [
        ("grafana", True),
        ("sentry", True),
    ]
    assert next(c for c in candidates if c["name"] == "sentry")["url"] == pins["sentry"]


# --- declared inputs: the form spec ---------------------------------------


def test_declared_header_inputs_pass_through_verbatim():
    # The remote's declared inputs reach the candidate untouched — the wire
    # response model owns their optionality, not the resolver.
    candidates = resolve_candidates([_GRAFANA], _PINS)

    assert candidates[0]["headers"] == [_GRAFANA_HEADER]


# --- the druks-side name ---------------------------------------------------


def test_derive_server_name_strips_noise_and_stays_identifier_safe():
    assert derive_server_name("io.github.grafana/mcp-grafana") == "grafana"
    assert derive_server_name("io.github.getsentry/sentry-mcp") == "sentry"
    assert derive_server_name("io.github.x/sentry-mcp-server") == "sentry"
    assert derive_server_name("com.mcparmory/sentry") == "sentry"
    assert derive_server_name("com.acme/linear.app") == "linear_app"
    # A digit-led remainder gets the mcp_ prefix back to stay letter-led;
    # an all-noise segment keeps its last token rather than emptying.
    assert derive_server_name("com.acme/3d-tools") == "mcp_3d_tools"
    assert derive_server_name("com.acme/mcp") == "mcp"


# --- the client: one GET, cached in Redis, loud on failure ------------------


@pytest.fixture(autouse=True)
def _fresh_search_cache():
    # The suite's FakeRedis lives for the whole session; drop this module's
    # keys so every test sees a cold cache.
    redis = get_client()
    redis._data = {
        key: value
        for key, value in redis._data.items()
        if not key.startswith(REGISTRY_SEARCH_CACHE_PREFIX)
    }


def _client_returning(handler):
    return lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_search_registry_fetches_latest_and_caches(monkeypatch):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"servers": [_GRAFANA], "metadata": {"count": 1}})

    monkeypatch.setattr(registry, "_http", _client_returning(handler))

    first = await search_registry("grafana")
    second = await search_registry("grafana")

    assert first == [_GRAFANA]
    assert second == [_GRAFANA]
    # One GET total — the second resolve reads the Redis cache; and that one
    # GET asked for latest versions only (the registry otherwise returns
    # every version of every server).
    assert len(requests) == 1
    assert requests[0].url.params["search"] == "grafana"
    assert requests[0].url.params["version"] == "latest"


async def test_search_registry_raises_typed_errors(monkeypatch):
    for response in (
        httpx.Response(500, text="boom"),
        httpx.Response(200, text="{not json"),
        httpx.Response(200, json={"unexpected": True}),
    ):
        monkeypatch.setattr(registry, "_http", _client_returning(lambda _r, r=response: r))
        with pytest.raises(RegistryUnavailableError, match="grafana"):
            await search_registry("grafana")


async def test_search_registry_result_feeds_the_resolver(monkeypatch):
    # The seam end to end on canned wire bytes: fetch → resolve → candidates.
    payload = {"servers": [_GRAFANA, _SENTRY, _entry("com.mcparmory/sentry")]}
    monkeypatch.setattr(
        registry,
        "_http",
        _client_returning(lambda _r: httpx.Response(200, text=json.dumps(payload))),
    )

    candidates = resolve_candidates(await search_registry("observability"), _PINS)

    assert [(c["name"], c["official"]) for c in candidates] == [
        ("grafana", True),
        ("sentry", True),
    ]

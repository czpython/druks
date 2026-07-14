import json

import httpx
import pytest
from conftest import configure_app_for_test, make_settings
from druks.mcp import registry
from druks.mcp.constants import REGISTRY_SEARCH_CACHE_PREFIX
from druks.mcp.exceptions import RegistryUnavailableError
from druks.mcp.models import McpOauthGrant, McpServer
from druks.mcp.registry import derive_server_name, resolve_candidates, search_registry
from druks.redis import get_client
from druks.settings import PACKAGED_MCP_TRUSTED
from fastapi.testclient import TestClient

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
            **({"remotes": remotes} if remotes else {}),
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

    grafana = candidates["io.github.grafana/mcp-grafana"]
    assert grafana["name"] == "grafana"
    assert grafana["official"] is True
    assert grafana["url"] == "https://mcp.grafana.com/mcp"
    assert grafana["description"].startswith("An MCP server")


def test_url_pin_lifts_sentry_whose_registry_entry_has_no_remote():
    # The registry omits sentry's hosted url; the pin supplies it. Display
    # text still comes from the registry entry.
    candidates = resolve_candidates([_SENTRY], _PINS)

    sentry = candidates["io.github.getsentry/sentry-mcp"]
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

    assert list(candidates) == ["io.github.getsentry/sentry-mcp"]
    assert "error monitoring" in candidates["io.github.getsentry/sentry-mcp"]["description"]


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

    assert candidates["com.cloudflare/browser"]["official"] is True


def test_unpinned_unmatched_remote_is_community():
    entries = [
        _entry(
            "io.github.someone/grafana-tools",
            remotes=[{"type": "streamable-http", "url": "https://tools.example.com/mcp"}],
        )
    ]

    candidates = resolve_candidates(entries, _PINS)

    assert candidates["io.github.someone/grafana-tools"]["official"] is False


def test_stdio_only_entries_are_dropped():
    # No streamable-http remote and no url pin — not installable, not shown.
    candidates = resolve_candidates([_entry("com.mcparmory/grafana")], _PINS)

    assert candidates == {}


def test_official_candidates_sort_first():
    community = _entry(
        "io.github.someone/aardvark",
        remotes=[{"type": "streamable-http", "url": "https://aardvark.example.com/mcp"}],
    )

    candidates = resolve_candidates([community, _GRAFANA], _PINS)

    assert [c["name"] for c in candidates.values()] == ["grafana", "aardvark"]


def test_packaged_pins_resolve_grafana_and_sentry():
    # The shipped trusted.json, end to end: grafana by publisher pin (registry
    # url kept), sentry by url pin (registry entry has no remote).
    pins = json.loads(PACKAGED_MCP_TRUSTED.read_text())

    candidates = resolve_candidates([_GRAFANA, _SENTRY], pins)

    assert [(c["name"], c["official"]) for c in candidates.values()] == [
        ("grafana", True),
        ("sentry", True),
    ]
    assert candidates["io.github.getsentry/sentry-mcp"]["url"] == pins["sentry"]


# --- declared inputs: the form spec ---------------------------------------


def test_declared_header_inputs_pass_through_verbatim():
    # The remote's declared inputs reach the candidate untouched — the wire
    # response model owns their optionality, not the resolver.
    candidates = resolve_candidates([_GRAFANA], _PINS)

    assert candidates["io.github.grafana/mcp-grafana"]["headers"] == [_GRAFANA_HEADER]


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

    assert [(c["name"], c["official"]) for c in candidates.values()] == [
        ("grafana", True),
        ("sentry", True),
    ]


# --- the install API --------------------------------------------------------

_ACME_ENTRY = _entry(
    "com.acme/observer",
    description="Observability for agents",
    remotes=[
        {
            "type": "streamable-http",
            "url": "https://mcp.acme.com/mcp",
            "headers": [
                {
                    "name": "X-Api-Key",
                    "description": "Acme API key",
                    "isSecret": True,
                    "isRequired": True,
                },
                {"name": "X-Region", "description": "Acme region"},
            ],
        }
    ],
)


def _client_with_registry(tmp_path, monkeypatch, *entries):
    payload = {"servers": list(entries)}
    monkeypatch.setattr(
        registry,
        "_http",
        _client_returning(lambda _r: httpx.Response(200, json=payload)),
    )
    app = configure_app_for_test(settings=make_settings(tmp_path, endpoint="http://druks.test"))
    return TestClient(app)


def test_registry_search_route_projects_resolved_candidates(tmp_path, monkeypatch, db_session):
    with _client_with_registry(tmp_path, monkeypatch, _GRAFANA, _SENTRY) as client:
        response = client.get("/api/mcp-servers/registry", params={"query": "observability"})

        assert response.status_code == 200
        grafana, sentry = response.json()
        assert grafana["name"] == "grafana"
        assert grafana["registryName"] == "io.github.grafana/mcp-grafana"
        assert grafana["official"] is True
        # Declared inputs ride verbatim — the registry owns their shape.
        assert grafana["headers"] == [_GRAFANA_HEADER]
        # The url-pinned sentry resolves with the pinned official url.
        assert sentry["url"] == "https://mcp.sentry.dev/mcp"


def test_registry_search_route_maps_unavailability_to_502(tmp_path, monkeypatch, db_session):
    monkeypatch.setattr(
        registry, "_http", _client_returning(lambda _r: httpx.Response(503, text="down"))
    )
    app = configure_app_for_test(settings=make_settings(tmp_path))
    with TestClient(app) as client:
        response = client.get("/api/mcp-servers/registry", params={"query": "grafana"})

        assert response.status_code == 502
        assert "registry search" in response.json()["detail"]


def test_add_from_registry_writes_the_row_and_redacts_the_secret(tmp_path, monkeypatch, db_session):
    with _client_with_registry(tmp_path, monkeypatch, _ACME_ENTRY) as client:
        created = client.post(
            "/api/mcp-servers/registry",
            json={
                "name": "observer",
                "registry": "com.acme/observer",
                "headers": {"X-Api-Key": "acme-api-secret", "X-Region": "eu"},
            },
        )

        assert created.status_code == 200
        body = created.json()
        # Header-auth'd: no bearer to miss, ready and enabled immediately.
        assert body["tokenSource"] == ""
        assert body["isEnabled"] is True
        assert body["hasToken"] is True
        assert "acme-api-secret" not in created.text

        listed = client.get("/api/mcp-servers")
        assert "acme-api-secret" not in listed.text

    # The row: url from the registry (never the client), values split by the
    # spec's secrecy — the plain one readable, the secret one ciphertext at
    # rest and redacted in repr.
    row = McpServer.get_by_name("observer")
    assert row.url == "https://mcp.acme.com/mcp"
    assert row.headers == {"X-Region": "eu"}
    assert "acme-api-secret" not in repr(row.secret_headers)
    assert row.secret_headers["X-Api-Key"] == "acme-api-secret"


def test_add_from_registry_oauth_candidate_ships_dark_and_connects(
    tmp_path, monkeypatch, db_session
):
    with _client_with_registry(tmp_path, monkeypatch, _GRAFANA) as client:
        created = client.post(
            "/api/mcp-servers/registry",
            json={
                "name": "grafana",
                "registry": "io.github.grafana/mcp-grafana",
                "headers": {"X-Grafana-URL": "https://acme.grafana.net"},
            },
        )

        assert created.status_code == 200
        body = created.json()
        assert body["tokenSource"] == "oauth"
        # Dark until its Connect lands — an enabled unconnected oauth server
        # would fail every delivery.
        assert body["isEnabled"] is False
        assert body["hasToken"] is False

        # The added row connects through the existing flow, against the row's
        # registry-supplied url.
        begun = []

        async def fake_begin_connect(name, server_url, endpoint):
            begun.append((name, server_url, endpoint))
            return "https://consent.example/authorize"

        monkeypatch.setattr("druks.mcp.oauth.begin_connect", fake_begin_connect)
        connect = client.post("/api/mcp-servers/grafana/connect")

        assert connect.status_code == 200
        assert connect.json()["authorizationUrl"] == "https://consent.example/authorize"
        assert begun == [("grafana", "https://mcp.grafana.com/mcp", "http://druks.test")]

    row = McpServer.get_by_name("grafana")
    assert row.headers == {"X-Grafana-URL": "https://acme.grafana.net"}


def test_add_from_registry_rejects_missing_required_and_unknown_headers(
    tmp_path, monkeypatch, db_session
):
    with _client_with_registry(tmp_path, monkeypatch, _ACME_ENTRY) as client:
        # Required X-Api-Key blank → named 422; nothing persisted.
        missing = client.post(
            "/api/mcp-servers/registry",
            json={
                "name": "observer",
                "registry": "com.acme/observer",
                "headers": {"X-Api-Key": "  ", "X-Region": "eu"},
            },
        )
        assert missing.status_code == 422
        assert "X-Api-Key" in missing.json()["detail"]

        # An unknown header is a client bug even when its value is blank.
        for bogus_value in ("v", ""):
            unknown = client.post(
                "/api/mcp-servers/registry",
                json={
                    "name": "observer",
                    "registry": "com.acme/observer",
                    "headers": {"X-Api-Key": "k", "X-Bogus": bogus_value},
                },
            )
            assert unknown.status_code == 422
            assert "X-Bogus" in unknown.json()["detail"]

        assert not McpServer.get_by_name("observer")


def test_add_from_registry_rejects_an_entry_without_an_http_remote(
    tmp_path, monkeypatch, db_session
):
    # stdio/oci-only and unpinned: resolvable in search, not installable.
    with _client_with_registry(tmp_path, monkeypatch, _entry("com.mcparmory/grafana")) as client:
        created = client.post(
            "/api/mcp-servers/registry",
            json={"name": "grafana", "registry": "com.mcparmory/grafana", "headers": {}},
        )

        assert created.status_code == 404
        assert "not installable" in created.json()["detail"]


def test_removing_a_connected_row_drops_its_grant(tmp_path, monkeypatch, db_session):
    with _client_with_registry(tmp_path, monkeypatch, _GRAFANA) as client:
        client.post(
            "/api/mcp-servers/registry",
            json={"name": "grafana", "registry": "io.github.grafana/mcp-grafana", "headers": {}},
        )
        McpOauthGrant.store(
            server_name="grafana",
            refresh_token="rt",
            token_endpoint="https://as.example/token",
            resource="https://mcp.grafana.com/mcp",
            client_id="cid",
        )

        assert client.delete("/api/mcp-servers/grafana").status_code == 204

    # An orphan grant would revive as this name's credential on re-add.
    assert not McpServer.get_by_name("grafana")
    assert not McpOauthGrant.get_by_server("grafana")

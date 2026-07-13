import json
from pathlib import Path

import pytest
from conftest import configure_app_for_test, make_settings
from druks.extensions.registry import mcp_servers
from druks.harnesses.claude import ClaudeHarness
from druks.harnesses.codex import CodexHarness
from druks.harnesses.datastructures import SandboxSettings
from druks.mcp.catalog import load_mcp_catalog
from druks.mcp.exceptions import (
    InvalidCatalogError,
    InvalidServerNameError,
    MissingTokenError,
    SourceEnvVarUnsetError,
)
from druks.mcp.helpers import get_bearer_token_env_var
from druks.mcp.models import McpServer
from druks.sandbox.datastructures import RequiredMcpServer, Workspace
from druks.settings import PACKAGED_MCP_CATALOG
from fastapi.testclient import TestClient

_LINEAR_URL = "https://mcp.linear.app/mcp"
_TOKEN = "lin_secret_value"


class _FakeSandbox:
    ssh_username = "exedev"


def _sandbox_config() -> SandboxSettings:
    return SandboxSettings(
        service_url="https://sb.test",
        service_token="t",
        service_timeout=30.0,
        image="img",
        claude_config_dir=Path("/home/agent/.claude"),
        codex_config_dir=Path("/home/agent/.codex"),
    )


async def _delivery(**kwargs) -> dict:
    # Delivery is resolved at the workspace seam: the enabled servers become wire
    # shapes on ``mcp_servers`` and their tokens land in ``extra_env``.
    return await Workspace(sandbox=_FakeSandbox()).with_mcp_servers(**kwargs)  # type: ignore[arg-type]


def _requiring_workspace(*servers: RequiredMcpServer) -> Workspace:
    # A workspace declaring the servers it requires and credentials itself,
    # as build does.
    class _Requiring(Workspace):
        def get_required_mcp_servers(self) -> tuple[RequiredMcpServer, ...]:
            return servers

    return _Requiring(sandbox=_FakeSandbox())  # type: ignore[arg-type]


# --- custom servers: CRUD + enable/disable -------------------------------


def test_create_lists_and_deletes(db_session):
    server = McpServer.create(name="linear", url=_LINEAR_URL, token=_TOKEN)

    by_name = McpServer.get_by_name("linear")
    assert by_name
    assert by_name.id == server.id
    assert "linear" in {s.name for s in McpServer.list_all()}

    server.delete()
    assert not McpServer.get_by_name("linear")


def test_enable_disable_moves_in_and_out_of_the_enabled_set(db_session):
    server = McpServer.create(name="linear", url=_LINEAR_URL, token=_TOKEN)
    assert "linear" in {s["name"] for s in McpServer.list_enabled()}

    server.is_enabled = False
    db_session.flush()
    assert "linear" not in {s["name"] for s in McpServer.list_enabled()}

    server.is_enabled = True
    db_session.flush()
    assert "linear" in {s["name"] for s in McpServer.list_enabled()}


# --- name validity: one identifier, shell/TOML-safe ----------------------


def test_create_rejects_names_that_break_env_or_config(db_session):
    # A hyphen breaks the sourced ``KEY='value'`` env line and the codex TOML key
    # path; a leading digit and uppercase are rejected for the same reason.
    for bad in ("linear-app", "1linear", "Linear", "linear.app", "linear app"):
        with pytest.raises(InvalidServerNameError, match="Invalid MCP server name"):
            McpServer.create(name=bad, url=_LINEAR_URL, token=_TOKEN)


def test_valid_name_derives_shell_safe_env_var(db_session):
    server = McpServer.create(name="linear_app", url=_LINEAR_URL, token=_TOKEN)
    # Every char of the derived var is a valid shell identifier char.
    var = get_bearer_token_env_var(server.name)
    assert var == "MCP_LINEAR_APP_TOKEN"
    assert all(ch.isalnum() or ch == "_" for ch in var)
    assert not var[0].isdigit()


# --- read paths: resolved view + enabled subset --------------------------


def test_list_enabled_carries_url_and_auth_for_delivery(db_session):
    # The enabled subset delivery renders from: url + token source per server.
    McpServer.create(name="linear", url=_LINEAR_URL, token=_TOKEN)

    linear = next(s for s in McpServer.list_enabled() if s["name"] == "linear")
    assert linear["url"] == _LINEAR_URL
    assert linear["token_source"] == "static"


# --- delivery at the workspace seam --------------------------------------


async def test_delivery_carries_static_token_in_env(db_session):
    McpServer.create(name="linear", url=_LINEAR_URL, token=_TOKEN)

    kwargs = await _delivery()
    assert "linear" in {s.name for s in kwargs["mcp_servers"]}
    # The token rides in env under the derived var; the wire shape names only the
    # var, never the value.
    assert kwargs["extra_env"][get_bearer_token_env_var("linear")] == _TOKEN
    linear = next(s for s in kwargs["mcp_servers"] if s.name == "linear")
    assert _TOKEN not in repr(linear)


async def test_required_server_delivers_beside_the_registry(db_session):
    # A workspace declares a server with a run-scoped token it minted itself
    # (build's per-repo reviewer token): wire shape + env var ride the same
    # seam as every registry server.
    McpServer.create(name="linear", url=_LINEAR_URL, token=_TOKEN)
    workspace = _requiring_workspace(
        RequiredMcpServer(
            name="github", url="https://api.githubcopilot.com/mcp/", token="ghs_minted"
        )
    )

    kwargs = await workspace.with_mcp_servers()

    github = next(s for s in kwargs["mcp_servers"] if s.name == "github")
    assert github.url == "https://api.githubcopilot.com/mcp/"
    assert kwargs["extra_env"][github.bearer_token_env_var] == "ghs_minted"
    assert "ghs_minted" not in repr(github)
    assert "linear" in {s.name for s in kwargs["mcp_servers"]}


async def test_required_server_owns_its_name_against_a_registry_twin(db_session):
    # Exactly one wire entry per name — the workspace's — and the registry twin
    # is skipped whole: its token neither clobbers the required server's
    # credential in env nor gets resolved at all (a tokenless twin would
    # otherwise raise).
    McpServer.create(name="linear", url=_LINEAR_URL, token=_TOKEN)
    McpServer.create(name="notion", url="https://mcp.notion.com/sse", token="")
    workspace = _requiring_workspace(
        RequiredMcpServer(
            name="linear", url="https://required.internal/linear", token="required-token"
        ),
        RequiredMcpServer(
            name="notion", url="https://required.internal/notion", token="notion-token"
        ),
    )

    kwargs = await workspace.with_mcp_servers()

    delivered = [s for s in kwargs["mcp_servers"] if s.name == "linear"]
    assert len(delivered) == 1
    assert delivered[0].url == "https://required.internal/linear"
    assert kwargs["extra_env"][get_bearer_token_env_var("linear")] == "required-token"
    assert kwargs["extra_env"][get_bearer_token_env_var("notion")] == "notion-token"


async def test_duplicate_required_names_are_refused(db_session):
    # Two servers under one name would collide in the emitted harness config
    # (one TOML table / JSON key per name) — refused loudly at delivery.
    workspace = _requiring_workspace(
        RequiredMcpServer(name="github", url="https://a/", token="t1"),
        RequiredMcpServer(name="github", url="https://b/", token="t2"),
    )

    with pytest.raises(ValueError, match="duplicate required"):
        await workspace.with_mcp_servers()


def test_required_server_token_stays_out_of_reprs():
    required = RequiredMcpServer(name="github", url="https://a/", token="ghs_secret")
    assert "ghs_secret" not in repr(required)


async def test_enabled_static_server_without_token_raises_loudly(db_session):
    # A tokenless enabled static row can't authenticate; delivery raises rather
    # than shipping a header the harness can't fill. (The API rejects creating
    # one; this guards the model-level path.)
    McpServer.create(name="notion", url="https://mcp.notion.com/sse", token="")

    with pytest.raises(MissingTokenError, match="notion"):
        await _delivery()


async def test_enabled_server_reaches_both_harness_configs_without_token(db_session):
    McpServer.create(name="linear", url=_LINEAR_URL, token=_TOKEN)
    kwargs = await _delivery()
    servers = kwargs["mcp_servers"]

    claude_config = " ".join(
        ClaudeHarness(
            model="claude-x", fast_mode=False, effort=None, sandbox=_sandbox_config()
        )._mcp_flags(servers)
    )
    assert _LINEAR_URL in claude_config
    assert get_bearer_token_env_var("linear") in claude_config
    assert _TOKEN not in claude_config

    codex_config = " ".join(
        CodexHarness(
            model=CodexHarness.models[0], fast_mode=False, effort=None, sandbox=_sandbox_config()
        )._mcp_flags(servers)
    )
    assert _LINEAR_URL in codex_config
    assert get_bearer_token_env_var("linear") in codex_config
    assert _TOKEN not in codex_config

    # The token lives only in the run env, keyed by the same var the config names.
    assert kwargs["extra_env"][get_bearer_token_env_var("linear")] == _TOKEN


async def test_delivery_tolerates_explicit_none_extra_env(db_session):
    # ``extra_env=None`` is valid for the underlying run_agent; the fold must treat
    # it like an omitted env, not unpack None (which would crash the call before it
    # starts). A static server still rides via its own delivery env.
    McpServer.create(name="linear", url=_LINEAR_URL, token=_TOKEN)

    kwargs = await _delivery(extra_env=None)
    assert "linear" in {s.name for s in kwargs["mcp_servers"]}
    assert kwargs["extra_env"][get_bearer_token_env_var("linear")] == _TOKEN


# --- declared headers: N per server, secret values via env refs -----------


def _grafana_shaped_server() -> None:
    # A registry-installed shape: no bearer (empty token_source), one plain
    # declared header and one secret one.
    McpServer.create(
        name="grafana",
        url="https://mcp.grafana.com/mcp",
        token_source="",
        headers={"X-Grafana-URL": "https://acme.grafana.net"},
        secret_headers={"X-Api-Key": "grafana-api-secret"},
    )


async def test_declared_headers_deliver_inline_and_secret_values_ride_env(db_session):
    _grafana_shaped_server()

    kwargs = await _delivery()

    grafana = next(s for s in kwargs["mcp_servers"] if s.name == "grafana")
    # The wire shape names the env var carrying each secret header; the value
    # rides only in the run env under that name, never inline.
    assert grafana.headers == {"X-Grafana-URL": "https://acme.grafana.net"}
    assert set(grafana.env_headers) == {"X-Api-Key"}
    header_env_var = grafana.env_headers["X-Api-Key"]
    assert kwargs["extra_env"][header_env_var] == "grafana-api-secret"
    assert "grafana-api-secret" not in repr(grafana)
    # No bearer: neither the wire shape nor the env carries an Authorization var.
    assert grafana.bearer_token_env_var == ""
    assert get_bearer_token_env_var("grafana") not in kwargs["extra_env"]


async def test_two_header_server_emits_both_headers_in_each_harness_config(db_session):
    _grafana_shaped_server()
    kwargs = await _delivery()
    servers = kwargs["mcp_servers"]
    header_env_var = servers[0].env_headers["X-Api-Key"]

    claude_flags = ClaudeHarness(
        model="claude-x", fast_mode=False, effort=None, sandbox=_sandbox_config()
    )._mcp_flags(servers)
    headers = json.loads(claude_flags[1])["mcpServers"]["grafana"]["headers"]
    assert headers == {
        "X-Grafana-URL": "https://acme.grafana.net",
        "X-Api-Key": f"${{{header_env_var}}}",
    }
    assert "grafana-api-secret" not in " ".join(claude_flags)

    codex_config = " ".join(
        CodexHarness(
            model=CodexHarness.models[0], fast_mode=False, effort=None, sandbox=_sandbox_config()
        )._mcp_flags(servers)
    )
    assert 'http_headers."X-Grafana-URL"="https://acme.grafana.net"' in codex_config
    assert f'env_http_headers."X-Api-Key"="{header_env_var}"' in codex_config
    assert "bearer_token_env_var" not in codex_config
    assert "grafana-api-secret" not in codex_config


async def test_bearer_and_declared_headers_combine_on_one_server(db_session):
    # A static-token server may also declare plain headers; the Authorization
    # bearer keeps its env-ref form beside them.
    McpServer.create(
        name="acme", url="https://mcp.acme.com/mcp", token=_TOKEN, headers={"X-Region": "eu"}
    )

    kwargs = await _delivery()
    servers = kwargs["mcp_servers"]

    claude_flags = ClaudeHarness(
        model="claude-x", fast_mode=False, effort=None, sandbox=_sandbox_config()
    )._mcp_flags(servers)
    headers = json.loads(claude_flags[1])["mcpServers"]["acme"]["headers"]
    assert headers == {
        "Authorization": f"Bearer ${{{get_bearer_token_env_var('acme')}}}",
        "X-Region": "eu",
    }
    assert kwargs["extra_env"][get_bearer_token_env_var("acme")] == _TOKEN


async def test_bearerless_server_delivers_without_a_bearer(db_session):
    # The loud MissingTokenError is a static-source contract; a bearerless
    # server (auth in its headers, or no auth) delivers without any bearer.
    McpServer.create(name="public_docs", url="https://docs.example.com/mcp", token_source="")

    kwargs = await _delivery()

    docs = next(s for s in kwargs["mcp_servers"] if s.name == "public_docs")
    assert docs.bearer_token_env_var == ""
    assert "extra_env" not in kwargs


def test_bearerless_server_resolves_ready_with_its_headers(db_session):
    _grafana_shaped_server()

    grafana = next(s for s in McpServer.list_resolved() if s["name"] == "grafana")
    assert grafana["token_source"] == ""
    assert grafana["headers"] == {"X-Grafana-URL": "https://acme.grafana.net"}
    # Nothing blocks delivery auth — the secret header is stored — so the
    # API reads it ready.
    assert grafana["has_token"] is True
    assert grafana["secret_headers"]["X-Api-Key"] == "grafana-api-secret"


# --- API: CRUD + enable/disable + redaction ------------------------------


def test_routes_crud_and_token_stays_backend_side(tmp_path, db_session):
    with TestClient(configure_app_for_test(settings=make_settings(tmp_path))) as client:
        created = client.post(
            "/api/mcp-servers", json={"name": "linear", "url": _LINEAR_URL, "token": _TOKEN}
        )
        assert created.status_code == 200
        body = created.json()
        assert body["name"] == "linear"
        assert body["builtin"] is False
        assert body["hasToken"] is True
        # The token never leaves the backend — the response carries only whether
        # one is set, never the value.
        assert _TOKEN not in created.text
        assert "token" not in body

        # Disable by name, then confirm it drops out of the enabled read path.
        toggled = client.patch("/api/mcp-servers/linear", json={"is_enabled": False})
        assert toggled.status_code == 200
        assert toggled.json()["isEnabled"] is False

        listed = client.get("/api/mcp-servers")
        assert _TOKEN not in listed.text
        linear = next(s for s in listed.json() if s["name"] == "linear")
        assert linear["isEnabled"] is False

        # Re-adding the same name is rejected — remove first.
        assert (
            client.post(
                "/api/mcp-servers", json={"name": "linear", "url": _LINEAR_URL, "token": _TOKEN}
            ).status_code
            == 409
        )

        assert client.delete("/api/mcp-servers/linear").status_code == 204
        assert not any(s["name"] == "linear" for s in client.get("/api/mcp-servers").json())


def test_routes_reject_invalid_name(tmp_path, db_session):
    with TestClient(configure_app_for_test(settings=make_settings(tmp_path))) as client:
        created = client.post(
            "/api/mcp-servers", json={"name": "linear-app", "url": _LINEAR_URL, "token": _TOKEN}
        )
        assert created.status_code == 422
        assert "Invalid MCP server name" in created.text


def test_routes_reject_creating_a_tokenless_custom_server(tmp_path, db_session):
    url = "https://mcp.notion.com/sse"
    with TestClient(configure_app_for_test(settings=make_settings(tmp_path))) as client:
        # A custom server is static; a blank (or whitespace-only) token would
        # create an enabled server that breaks every run at delivery. Rejected at
        # the boundary instead.
        for body in ({"name": "notion", "url": url}, {"name": "notion", "url": url, "token": "  "}):
            created = client.post("/api/mcp-servers", json=body)
            assert created.status_code == 422
            assert "bearer token" in created.text
        assert not any(s["name"] == "notion" for s in client.get("/api/mcp-servers").json())


def test_routes_reject_creating_a_urlless_custom_server(tmp_path, db_session):
    with TestClient(configure_app_for_test(settings=make_settings(tmp_path))) as client:
        # A blank (or whitespace-only) url is an unreachable endpoint that would
        # ship into every VM; rejected server-side, not just disabled in the UI.
        for bad_url in ("", "   "):
            created = client.post(
                "/api/mcp-servers", json={"name": "notion", "url": bad_url, "token": _TOKEN}
            )
            assert created.status_code == 422
            assert "needs a url" in created.text
        assert not any(s["name"] == "notion" for s in client.get("/api/mcp-servers").json())


def test_routes_reject_adding_a_builtin(tmp_path, registry_state, db_session):
    load_mcp_catalog(_write_catalog(tmp_path, {"figma_test": _static_entry("https://f/")}))
    with TestClient(configure_app_for_test(settings=make_settings(tmp_path))) as client:
        # A catalog entry is built-in — you configure it, you don't add it.
        created = client.post(
            "/api/mcp-servers", json={"name": "figma_test", "url": "https://x", "token": "t"}
        )
        assert created.status_code == 409
        assert "built-in" in created.text


def test_routes_disable_and_refuse_deleting_a_builtin(tmp_path, registry_state, db_session):
    load_mcp_catalog(_write_catalog(tmp_path, {"figma_test": _static_entry("https://f/")}))
    with TestClient(configure_app_for_test(settings=make_settings(tmp_path))) as client:
        figma = next(s for s in client.get("/api/mcp-servers").json() if s["name"] == "figma_test")
        assert figma["builtin"] is True
        assert figma["isEnabled"] is True

        # Backend-owned entry: delete is refused, disable is offered instead.
        assert client.delete("/api/mcp-servers/figma_test").status_code == 409

        disabled = client.patch("/api/mcp-servers/figma_test", json={"is_enabled": False})
        assert disabled.status_code == 200
        assert disabled.json()["isEnabled"] is False
        # The overlay row now exists and the entry reads disabled everywhere.
        assert McpServer.get_by_name("figma_test")


# --- catalog: the deploy-declarative default-server set -------------------


@pytest.fixture
def registry_state():
    # Catalog loads register into the process-global registry; snapshot and
    # restore so a test's entries don't leak into the rest of the suite.
    saved = dict(mcp_servers._items)
    yield
    mcp_servers._items.clear()
    mcp_servers._items.update(saved)


def _write_catalog(tmp_path, content):
    path = tmp_path / "catalog.json"
    path.write_text(content if isinstance(content, str) else json.dumps(content))
    return path


def _env_entry(url="https://mcp.vault.test/", env="VAULT_TEST_TOKEN"):
    return {"url": url, "auth": {"type": "static_from_env", "env": env}}


def _static_entry(url):
    return {"url": url, "auth": {"type": "static"}}


def test_packaged_catalog_ships_linear_disabled(registry_state, db_session):
    # The one packaged default: Linear's hosted MCP, shipped dark — an oauth
    # entry has no grant until an operator connects it, and an enabled
    # unconnected one would fail every run's delivery. build's github MCP is
    # build's own requirement (get_required_mcp_servers), never a catalog entry.
    load_mcp_catalog(PACKAGED_MCP_CATALOG)

    assert "github" not in mcp_servers
    builtins = [s for s in McpServer.list_resolved() if s["builtin"]]
    assert [s["name"] for s in builtins] == ["linear"]
    linear = builtins[0]
    assert linear["url"] == "https://mcp.linear.app/mcp"
    assert linear["token_source"] == "oauth"
    assert linear["is_enabled"] is False
    assert "linear" not in {s["name"] for s in McpServer.list_enabled()}


async def test_packaged_catalog_delivers_nothing_until_linear_is_connected(
    registry_state, db_session
):
    # The regression the disabled default exists for: a fresh install has no
    # grant, and delivery fails loudly for an enabled unconnected oauth server
    # (test_delivery_fails_loudly_for_an_unconnected_enabled_oauth_server) —
    # disabled, linear is simply not delivered and runs work out of the box.
    load_mcp_catalog(PACKAGED_MCP_CATALOG)

    kwargs = await _delivery()
    assert "mcp_servers" not in kwargs


def test_packaged_linear_enables_like_any_builtin(tmp_path, registry_state, db_session):
    load_mcp_catalog(PACKAGED_MCP_CATALOG)
    with TestClient(configure_app_for_test(settings=make_settings(tmp_path))) as client:
        listed = next(s for s in client.get("/api/mcp-servers").json() if s["name"] == "linear")
        assert listed["builtin"] is True
        assert listed["isEnabled"] is False
        assert listed["tokenSource"] == "oauth"
        assert listed["hasToken"] is False

        enabled = client.patch("/api/mcp-servers/linear", json={"is_enabled": True})
        assert enabled.status_code == 200
        assert enabled.json()["isEnabled"] is True
        # The operator's overlay row now carries the choice; the shipped
        # default only ever applied while no row existed.
        assert McpServer.get_by_name("linear")


def test_load_catalog_tolerates_wrapper_and_is_idempotent(tmp_path, registry_state):
    catalog = {"mcpServers": {"stripe_test": _static_entry("https://mcp.stripe.test/")}}
    path = _write_catalog(tmp_path, catalog)

    load_mcp_catalog(path)
    # A second boot in the same process re-loads the same file; equal entries
    # are skipped, not collided.
    load_mcp_catalog(path)

    assert mcp_servers.get("stripe_test")["url"] == "https://mcp.stripe.test/"


def test_load_catalog_accepts_a_bare_map(tmp_path, registry_state):
    load_mcp_catalog(_write_catalog(tmp_path, {"bare_test": _static_entry("https://x/")}))
    assert "bare_test" in mcp_servers


def test_load_catalog_collides_on_a_changed_definition_and_mounts_nothing(tmp_path, registry_state):
    load_mcp_catalog(_write_catalog(tmp_path, {"dup_test": _static_entry("https://a/")}))

    conflicting = {
        "other_test": _static_entry("https://ok/"),
        "dup_test": _static_entry("https://b/"),
    }
    with pytest.raises(InvalidCatalogError, match="dup_test"):
        load_mcp_catalog(_write_catalog(tmp_path, conflicting))

    # The failed load is atomic: the original definition survives and the valid
    # sibling entry (listed before the colliding one) was not registered.
    assert mcp_servers.get("dup_test")["url"] == "https://a/"
    assert "other_test" not in mcp_servers


def test_load_catalog_fails_loudly_on_bad_content(tmp_path, registry_state):
    # Each malformed shape stops the load with the path + the failing server's
    # name and field — never a silent drop of servers from every agent VM. The
    # entry models parse strictly (extra = forbid), so a typo'd key fails too.
    for content, reason in (
        ("{not json", "not valid JSON"),
        ('["list"]', "top level"),
        ({"mcpServers": ["list"]}, "mcpServers"),
        ({"Bad-Name": {"url": "https://x/", "auth": {"type": "static"}}}, "invalid server name"),
        ({"x": "not-an-object"}, "dictionary"),
        ({"x": {"auth": {"type": "static"}}}, "url"),
        ({"x": {"url": "  ", "auth": {"type": "static"}}}, "url"),
        ({"x": {"url": "https://x/", "transport": "stdio", "auth": {"type": "static"}}}, "stdio"),
        ({"x": {"url": "https://x/"}}, "auth"),
        ({"x": {"url": "https://x/", "auth": {"type": "magic"}}}, "magic"),
        ({"x": {"url": "https://x/", "auth": {"type": "static_from_env"}}}, "env"),
        ({"x": {"url": "https://x/", "auth": {"type": "static", "env": "FOO"}}}, "env"),
        ({"x": {"url": "https://x/", "trasport": "http", "auth": {"type": "static"}}}, "trasport"),
    ):
        with pytest.raises(InvalidCatalogError, match=reason):
            load_mcp_catalog(_write_catalog(tmp_path, content))


def test_load_catalog_missing_file_fails_loudly(tmp_path):
    with pytest.raises(InvalidCatalogError, match="absent.json"):
        load_mcp_catalog(tmp_path / "absent.json")


def test_db_overlay_still_disables_a_catalog_entry(tmp_path, registry_state, db_session):
    load_mcp_catalog(
        _write_catalog(tmp_path, {"figma_test": _static_entry("https://mcp.figma.test/")})
    )

    McpServer.create(name="figma_test", url="https://mcp.figma.test/", is_enabled=False)

    resolved = next(s for s in McpServer.list_resolved() if s["name"] == "figma_test")
    assert resolved["builtin"] is True
    assert "figma_test" not in {s["name"] for s in McpServer.list_enabled()}


def test_catalog_enabled_false_ships_the_entry_dark(tmp_path, registry_state, db_session):
    # ``enabled`` is the catalog's shipped default, not operator state: false
    # resolves disabled until an operator row says otherwise; an entry without
    # the key stays enabled exactly as before the field existed.
    load_mcp_catalog(
        _write_catalog(
            tmp_path,
            {
                "dark_test": {**_static_entry("https://d/"), "enabled": False},
                "lit_test": _static_entry("https://l/"),
            },
        )
    )

    resolved = {s["name"]: s for s in McpServer.list_resolved()}
    assert resolved["dark_test"]["is_enabled"] is False
    assert resolved["lit_test"]["is_enabled"] is True
    enabled_names = {s["name"] for s in McpServer.list_enabled()}
    assert "dark_test" not in enabled_names
    assert "lit_test" in enabled_names


# --- static-from-env: the token lives in druks' own process env -----------


async def test_static_from_env_delivers_the_token_from_process_env(
    tmp_path, registry_state, monkeypatch, db_session
):
    load_mcp_catalog(_write_catalog(tmp_path, {"vault_test": _env_entry()}))
    monkeypatch.setenv("VAULT_TEST_TOKEN", "vault-secret")

    kwargs = await _delivery()

    # The value rides only in env, under the derived var the config names; the
    # wire shape never carries it.
    assert kwargs["extra_env"][get_bearer_token_env_var("vault_test")] == "vault-secret"
    vault = next(s for s in kwargs["mcp_servers"] if s.name == "vault_test")
    assert vault.url == "https://mcp.vault.test/"
    assert "vault-secret" not in repr(vault)


async def test_static_from_env_unset_var_fails_loudly_at_delivery(
    tmp_path, registry_state, monkeypatch, db_session
):
    load_mcp_catalog(_write_catalog(tmp_path, {"vault_test": _env_entry()}))
    monkeypatch.delenv("VAULT_TEST_TOKEN", raising=False)

    with pytest.raises(SourceEnvVarUnsetError, match="VAULT_TEST_TOKEN"):
        await _delivery()


async def test_definition_auth_wins_over_an_overlay_row_token(
    tmp_path, registry_state, monkeypatch, db_session
):
    # Precedence: for a catalog-managed name the definition's auth strategy
    # decides how the token is sourced — a row token is inert for env-sourced
    # entries, and druks never needs one stored.
    load_mcp_catalog(_write_catalog(tmp_path, {"vault_test": _env_entry()}))
    McpServer.create(name="vault_test", url="https://mcp.vault.test/", token="db-token")
    monkeypatch.setenv("VAULT_TEST_TOKEN", "env-token")

    kwargs = await _delivery()

    assert kwargs["extra_env"][get_bearer_token_env_var("vault_test")] == "env-token"


def test_api_has_token_reflects_env_presence_for_env_sourced(
    tmp_path, registry_state, monkeypatch, db_session
):
    load_mcp_catalog(_write_catalog(tmp_path, {"vault_test": _env_entry()}))
    monkeypatch.delenv("VAULT_TEST_TOKEN", raising=False)

    with TestClient(configure_app_for_test(settings=make_settings(tmp_path))) as client:
        vault = next(s for s in client.get("/api/mcp-servers").json() if s["name"] == "vault_test")
        assert vault["hasToken"] is False
        # The badge can name the var to set — a var name, never a value.
        assert vault["sourceEnvVar"] == "VAULT_TEST_TOKEN"

        monkeypatch.setenv("VAULT_TEST_TOKEN", "vault-secret")
        listed = client.get("/api/mcp-servers")
        vault = next(s for s in listed.json() if s["name"] == "vault_test")
        assert vault["hasToken"] is True
        assert "vault-secret" not in listed.text

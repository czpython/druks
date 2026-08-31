import json
from pathlib import Path

import pytest
from druks.apps.registry import mcp_servers
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
from druks.sandbox.datastructures import RequiredMcpServer
from druks.settings import PACKAGED_MCP_CATALOG
from druks.testing import asgi_client, configure_app_for_test, make_settings
from druks.workspaces import Workspace

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
    return await Workspace(host=_FakeSandbox()).with_mcp_servers(  # type: ignore[arg-type]
        None, **kwargs
    )


def _requiring_workspace(*servers: RequiredMcpServer) -> Workspace:
    # A workspace declaring the servers it requires and credentials itself,
    # as SoftwareFactory does.
    class _Requiring(Workspace):
        def get_required_mcp_servers(self) -> tuple[RequiredMcpServer, ...]:
            return servers

    return _Requiring(host=_FakeSandbox())  # type: ignore[arg-type]


# --- custom servers: CRUD + enable/disable -------------------------------


async def test_create_lists_and_deletes(druks_db):
    server = await McpServer.create(name="linear", url=_LINEAR_URL, token=_TOKEN)

    by_name = await McpServer.get_for_name("linear")
    assert by_name
    assert by_name.id == server.id
    assert "linear" in {s.name for s in await McpServer.list_all()}

    await server.delete()
    assert not await McpServer.get_for_name("linear")


async def test_enable_disable_moves_in_and_out_of_the_enabled_set(druks_db):
    server = await McpServer.create(name="linear", url=_LINEAR_URL, token=_TOKEN)
    assert "linear" in {s["name"] for s in await McpServer.list_enabled()}

    server.is_enabled = False
    await druks_db.flush()
    assert "linear" not in {s["name"] for s in await McpServer.list_enabled()}

    server.is_enabled = True
    await druks_db.flush()
    assert "linear" in {s["name"] for s in await McpServer.list_enabled()}


# --- name validity: one identifier, shell/TOML-safe ----------------------


async def test_create_rejects_names_that_break_env_or_config(druks_db):
    # A hyphen breaks the sourced ``KEY='value'`` env line and the codex TOML key
    # path; a leading digit and uppercase are rejected for the same reason.
    for bad in ("linear-app", "1linear", "Linear", "linear.app", "linear app"):
        with pytest.raises(InvalidServerNameError, match="Invalid MCP server name"):
            await McpServer.create(name=bad, url=_LINEAR_URL, token=_TOKEN)


async def test_valid_name_derives_shell_safe_env_var(druks_db):
    server = await McpServer.create(name="linear_app", url=_LINEAR_URL, token=_TOKEN)
    # Every char of the derived var is a valid shell identifier char.
    var = get_bearer_token_env_var(server.name)
    assert var == "MCP_LINEAR_APP_TOKEN"
    assert all(ch.isalnum() or ch == "_" for ch in var)
    assert not var[0].isdigit()


# --- delivery at the workspace seam --------------------------------------


async def test_delivery_carries_static_token_in_env(druks_db):
    await McpServer.create(name="linear", url=_LINEAR_URL, token=_TOKEN)

    kwargs = await _delivery()
    assert "linear" in {s.name for s in kwargs["mcp_servers"]}
    # The token rides in env under the derived var; the wire shape names only the
    # var, never the value.
    assert kwargs["extra_env"][get_bearer_token_env_var("linear")] == _TOKEN
    linear = next(s for s in kwargs["mcp_servers"] if s.name == "linear")
    assert _TOKEN not in repr(linear)


async def test_required_server_delivers_beside_the_registry(druks_db):
    # A workspace declares a server with a run-scoped token it minted itself
    # (SoftwareFactory's per-repo reviewer token): wire shape + env var ride the same
    # seam as every registry server.
    await McpServer.create(name="linear", url=_LINEAR_URL, token=_TOKEN)
    workspace = _requiring_workspace(
        RequiredMcpServer(
            name="github", url="https://api.githubcopilot.com/mcp/", token="ghs_minted"
        )
    )

    kwargs = await workspace.with_mcp_servers(None)

    github = next(s for s in kwargs["mcp_servers"] if s.name == "github")
    assert github.url == "https://api.githubcopilot.com/mcp/"
    assert kwargs["extra_env"][github.bearer_token_env_var] == "ghs_minted"
    assert "ghs_minted" not in repr(github)
    assert "linear" in {s.name for s in kwargs["mcp_servers"]}


async def test_required_server_owns_its_name_against_a_registry_twin(druks_db):
    # Exactly one wire entry per name — the workspace's — and the registry twin
    # is skipped whole: its token neither clobbers the required server's
    # credential in env nor gets resolved at all (a tokenless twin would
    # otherwise raise).
    await McpServer.create(name="linear", url=_LINEAR_URL, token=_TOKEN)
    await McpServer.create(name="notion", url="https://mcp.notion.com/sse", token="")
    workspace = _requiring_workspace(
        RequiredMcpServer(
            name="linear", url="https://required.internal/linear", token="required-token"
        ),
        RequiredMcpServer(
            name="notion", url="https://required.internal/notion", token="notion-token"
        ),
    )

    kwargs = await workspace.with_mcp_servers(None)

    delivered = [s for s in kwargs["mcp_servers"] if s.name == "linear"]
    assert len(delivered) == 1
    assert delivered[0].url == "https://required.internal/linear"
    assert kwargs["extra_env"][get_bearer_token_env_var("linear")] == "required-token"
    assert kwargs["extra_env"][get_bearer_token_env_var("notion")] == "notion-token"


async def test_duplicate_required_names_are_refused(druks_db):
    # Two servers under one name would collide in the emitted harness config
    # (one TOML table / JSON key per name) — refused loudly at delivery.
    workspace = _requiring_workspace(
        RequiredMcpServer(name="github", url="https://a/", token="t1"),
        RequiredMcpServer(name="github", url="https://b/", token="t2"),
    )

    with pytest.raises(ValueError, match="duplicate required"):
        await workspace.with_mcp_servers(None)


def test_required_server_token_stays_out_of_reprs():
    required = RequiredMcpServer(name="github", url="https://a/", token="ghs_secret")
    assert "ghs_secret" not in repr(required)


async def test_enabled_static_server_without_token_raises_loudly(druks_db):
    # A tokenless enabled static row can't authenticate; delivery raises rather
    # than shipping a header the harness can't fill. (The API rejects creating
    # one; this guards the model-level path.)
    await McpServer.create(name="notion", url="https://mcp.notion.com/sse", token="")

    with pytest.raises(MissingTokenError, match="notion"):
        await _delivery()


async def test_enabled_server_reaches_both_harness_configs_without_token(druks_db):
    await McpServer.create(name="linear", url=_LINEAR_URL, token=_TOKEN)
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


async def test_delivery_tolerates_explicit_none_extra_env(druks_db):
    # ``extra_env=None`` is valid for the underlying run_agent; the fold must treat
    # it like an omitted env, not unpack None (which would crash the call before it
    # starts). A static server still rides via its own delivery env.
    await McpServer.create(name="linear", url=_LINEAR_URL, token=_TOKEN)

    kwargs = await _delivery(extra_env=None)
    assert "linear" in {s.name for s in kwargs["mcp_servers"]}
    assert kwargs["extra_env"][get_bearer_token_env_var("linear")] == _TOKEN


# --- declared headers: N per server, secret values via env refs -----------


async def _grafana_shaped_server() -> None:
    # A registry-installed shape: no bearer (empty token_source), one plain
    # declared header and one secret one.
    await McpServer.create(
        name="grafana",
        url="https://mcp.grafana.com/mcp",
        token_source="",
        headers={"X-Grafana-URL": "https://acme.grafana.net"},
        secret_headers={"X-Api-Key": "grafana-api-secret"},
    )


async def test_declared_headers_deliver_inline_and_secret_values_ride_env(druks_db):
    await _grafana_shaped_server()

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


async def test_two_header_server_emits_both_headers_in_each_harness_config(druks_db):
    await _grafana_shaped_server()
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


async def test_bearer_and_declared_headers_combine_on_one_server(druks_db):
    # A static-token server may also declare plain headers; the Authorization
    # bearer keeps its env-ref form beside them.
    await McpServer.create(
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


async def test_bearerless_server_delivers_without_a_bearer(druks_db):
    # The loud MissingTokenError is a static-source contract; a bearerless
    # server (auth in its headers, or no auth) delivers without any bearer.
    await McpServer.create(name="public_docs", url="https://docs.example.com/mcp", token_source="")

    kwargs = await _delivery()

    docs = next(s for s in kwargs["mcp_servers"] if s.name == "public_docs")
    assert docs.bearer_token_env_var == ""
    assert "extra_env" not in kwargs


async def test_bearerless_server_merges_with_its_headers(druks_db):
    await _grafana_shaped_server()

    grafana = (await McpServer._merged())["grafana"]
    assert grafana["token_source"] == ""
    assert grafana["headers"] == {"X-Grafana-URL": "https://acme.grafana.net"}
    assert grafana["secret_headers"]["X-Api-Key"] == "grafana-api-secret"


# --- API: CRUD + enable/disable + redaction ------------------------------


async def test_routes_crud_and_token_stays_backend_side(tmp_path, druks_db):
    async with asgi_client(configure_app_for_test(settings=make_settings(tmp_path))) as client:
        created = await client.post(
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
        toggled = await client.patch("/api/mcp-servers/linear", json={"is_enabled": False})
        assert toggled.status_code == 200
        assert toggled.json()["isEnabled"] is False

        listed = await client.get("/api/mcp-servers")
        assert _TOKEN not in listed.text
        linear = next(s for s in listed.json() if s["name"] == "linear")
        assert linear["isEnabled"] is False

        # Re-adding the same name is rejected — remove first.
        assert (
            await client.post(
                "/api/mcp-servers", json={"name": "linear", "url": _LINEAR_URL, "token": _TOKEN}
            )
        ).status_code == 409

        assert (await client.delete("/api/mcp-servers/linear")).status_code == 204
        assert not any(s["name"] == "linear" for s in (await client.get("/api/mcp-servers")).json())


async def test_routes_reject_invalid_name(tmp_path, druks_db):
    async with asgi_client(configure_app_for_test(settings=make_settings(tmp_path))) as client:
        created = await client.post(
            "/api/mcp-servers", json={"name": "linear-app", "url": _LINEAR_URL, "token": _TOKEN}
        )
        assert created.status_code == 422
        assert "Invalid MCP server name" in created.text


async def test_routes_reject_creating_a_tokenless_custom_server(tmp_path, druks_db):
    url = "https://mcp.notion.com/sse"
    async with asgi_client(configure_app_for_test(settings=make_settings(tmp_path))) as client:
        # A custom server is static; a blank (or whitespace-only) token would
        # create an enabled server that breaks every run at delivery. Rejected at
        # the boundary instead.
        for body in ({"name": "notion", "url": url}, {"name": "notion", "url": url, "token": "  "}):
            created = await client.post("/api/mcp-servers", json=body)
            assert created.status_code == 422
            assert "bearer token" in created.text
        assert not any(s["name"] == "notion" for s in (await client.get("/api/mcp-servers")).json())


async def test_routes_reject_creating_a_urlless_custom_server(tmp_path, druks_db):
    async with asgi_client(configure_app_for_test(settings=make_settings(tmp_path))) as client:
        # A blank (or whitespace-only) url is an unreachable endpoint that would
        # ship into every VM; rejected server-side, not just disabled in the UI.
        for bad_url in ("", "   "):
            created = await client.post(
                "/api/mcp-servers", json={"name": "notion", "url": bad_url, "token": _TOKEN}
            )
            assert created.status_code == 422
            assert "needs a url" in created.text
        assert not any(s["name"] == "notion" for s in (await client.get("/api/mcp-servers")).json())


async def test_routes_reject_adding_a_builtin(tmp_path, registry_state, druks_db):
    load_mcp_catalog(_write_catalog(tmp_path, {"figma_test": _static_entry("https://f/")}))
    async with asgi_client(configure_app_for_test(settings=make_settings(tmp_path))) as client:
        # A catalog entry is built-in — you configure it, you don't add it.
        created = await client.post(
            "/api/mcp-servers", json={"name": "figma_test", "url": "https://x", "token": "t"}
        )
        assert created.status_code == 409
        assert "built-in" in created.text


async def test_routes_disable_and_refuse_deleting_a_builtin(tmp_path, registry_state, druks_db):
    load_mcp_catalog(_write_catalog(tmp_path, {"figma_test": _static_entry("https://f/")}))
    async with asgi_client(configure_app_for_test(settings=make_settings(tmp_path))) as client:
        figma = next(
            s for s in (await client.get("/api/mcp-servers")).json() if s["name"] == "figma_test"
        )
        assert figma["builtin"] is True
        assert figma["isEnabled"] is True

        # Backend-owned entry: delete is refused, disable is offered instead.
        assert (await client.delete("/api/mcp-servers/figma_test")).status_code == 409

        disabled = await client.patch("/api/mcp-servers/figma_test", json={"is_enabled": False})
        assert disabled.status_code == 200
        assert disabled.json()["isEnabled"] is False
        # The overlay row now exists and the entry reads disabled everywhere.
        assert await McpServer.get_for_name("figma_test")


# --- catalog: the deploy-declarative default-server set -------------------


def _write_catalog(tmp_path, content):
    path = tmp_path / "catalog.json"
    path.write_text(content if isinstance(content, str) else json.dumps(content))
    return path


def _env_entry(url="https://mcp.vault.test/", env="VAULT_TEST_TOKEN"):
    return {"url": url, "auth": {"type": "static_from_env", "env": env}}


def _static_entry(url):
    return {"url": url, "auth": {"type": "static"}}


async def test_packaged_catalog_is_empty_and_delivers_nothing(registry_state, druks_db):
    # The packaged default is an explicit empty ``mcpServers`` map: a fresh
    # install registers no built-ins and delivers no MCP servers. SoftwareFactory's github
    # MCP is SoftwareFactory's own requirement (get_required_mcp_servers), never a catalog
    # entry.
    load_mcp_catalog(PACKAGED_MCP_CATALOG)

    assert not [s for s in (await McpServer._merged()).values() if s["builtin"]]

    kwargs = await _delivery()
    assert "mcp_servers" not in kwargs


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


async def test_db_overlay_still_disables_a_catalog_entry(tmp_path, registry_state, druks_db):
    load_mcp_catalog(
        _write_catalog(tmp_path, {"figma_test": _static_entry("https://mcp.figma.test/")})
    )

    await McpServer.create(name="figma_test", url="https://mcp.figma.test/", is_enabled=False)

    resolved = (await McpServer._merged())["figma_test"]
    assert resolved["builtin"] is True
    assert "figma_test" not in {s["name"] for s in await McpServer.list_enabled()}


async def test_catalog_enabled_false_ships_the_entry_dark(tmp_path, registry_state, druks_db):
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

    resolved = await McpServer._merged()
    assert resolved["dark_test"]["is_enabled"] is False
    assert resolved["lit_test"]["is_enabled"] is True
    enabled_names = {s["name"] for s in await McpServer.list_enabled()}
    assert "dark_test" not in enabled_names
    assert "lit_test" in enabled_names


# --- static-from-env: the token lives in druks' own process env -----------


async def test_static_from_env_delivers_the_token_from_process_env(
    tmp_path, registry_state, monkeypatch, druks_db
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
    tmp_path, registry_state, monkeypatch, druks_db
):
    load_mcp_catalog(_write_catalog(tmp_path, {"vault_test": _env_entry()}))
    monkeypatch.delenv("VAULT_TEST_TOKEN", raising=False)

    with pytest.raises(SourceEnvVarUnsetError, match="VAULT_TEST_TOKEN"):
        await _delivery()


async def test_definition_auth_wins_over_an_overlay_row_token(
    tmp_path, registry_state, monkeypatch, druks_db
):
    # Precedence: for a catalog-managed name the definition's auth strategy
    # decides how the token is sourced — a row token is inert for env-sourced
    # entries, and druks never needs one stored.
    load_mcp_catalog(_write_catalog(tmp_path, {"vault_test": _env_entry()}))
    await McpServer.create(name="vault_test", url="https://mcp.vault.test/", token="db-token")
    monkeypatch.setenv("VAULT_TEST_TOKEN", "env-token")

    kwargs = await _delivery()

    assert kwargs["extra_env"][get_bearer_token_env_var("vault_test")] == "env-token"


async def test_api_has_token_reflects_env_presence_for_env_sourced(
    tmp_path, registry_state, monkeypatch, druks_db
):
    load_mcp_catalog(_write_catalog(tmp_path, {"vault_test": _env_entry()}))
    monkeypatch.delenv("VAULT_TEST_TOKEN", raising=False)

    async with asgi_client(configure_app_for_test(settings=make_settings(tmp_path))) as client:
        vault = next(
            s for s in (await client.get("/api/mcp-servers")).json() if s["name"] == "vault_test"
        )
        assert vault["hasToken"] is False
        # The badge can name the var to set — a var name, never a value.
        assert vault["sourceEnvVar"] == "VAULT_TEST_TOKEN"

        monkeypatch.setenv("VAULT_TEST_TOKEN", "vault-secret")
        listed = await client.get("/api/mcp-servers")
        vault = next(s for s in listed.json() if s["name"] == "vault_test")
        assert vault["hasToken"] is True
        assert "vault-secret" not in listed.text

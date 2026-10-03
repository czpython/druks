import json

from druks.mcp.constants import NAME_PATTERN
from druks.mcp.enums import IdentityMode
from druks.mcp.models import McpServer
from druks.secrets.datastructures import Audience
from druks.secrets.models import VaultSecret
from druks.settings import PACKAGED_MCP_DIRECTORY
from druks.testing import configure_app_for_test, make_settings
from fastapi.testclient import TestClient


def _client(tmp_path):
    app = configure_app_for_test(
        settings=make_settings(tmp_path, urls={"endpoint": "http://druks.test"})
    )
    return TestClient(app)


def test_the_directory_serves_the_packaged_servers(tmp_path, druks_db):
    with _client(tmp_path) as client:
        listed = client.get("/api/mcp-servers/directory").json()

    by_name = {server["name"]: server for server in listed}
    assert by_name.keys() == json.loads(PACKAGED_MCP_DIRECTORY.read_text()).keys()
    assert by_name["grafana"]["title"] == "Grafana Cloud"
    for server in listed:
        assert NAME_PATTERN.match(server["name"])
        assert server["url"].startswith("https://")
        assert server["title"] and server["description"]


async def test_adding_a_directory_server_ships_dark_and_connects(tmp_path, monkeypatch, druks_db):
    with _client(tmp_path) as client:
        created = client.post("/api/mcp-servers/directory", json={"name": "grafana"})

        assert created.status_code == 200
        body = created.json()
        assert body["isOauth"] is True
        # Dark until its Connect lands — an enabled unconnected oauth server
        # would fail every delivery.
        assert body["isEnabled"] is False
        assert body["hasToken"] is False

        begun = []

        async def fake_begin_connect(name, server_url, endpoint, *, account_id, identity_mode):
            begun.append((name, server_url, endpoint, identity_mode))
            return "https://consent.example/authorize"

        monkeypatch.setattr("druks.mcp.oauth.begin_connect", fake_begin_connect)
        connect = client.post(
            "/api/mcp-servers/grafana/connect", json={"identity_mode": IdentityMode.PER_USER}
        )

        assert connect.json()["authorizationUrl"] == "https://consent.example/authorize"
        assert begun == [
            ("grafana", "https://mcp.grafana.com/mcp", "http://druks.test", IdentityMode.PER_USER)
        ]

    row = await McpServer.get_for_name(druks_db, "grafana")
    assert row.url == "https://mcp.grafana.com/mcp"


async def test_github_connects_through_the_service(tmp_path, druks_db):
    with _client(tmp_path) as client:
        created = client.post("/api/mcp-servers/directory", json={"name": "github"})

        body = created.json()
        assert (body["isOauth"], body["isEnabled"]) == (True, False)
        assert (body["credential"], body["service"]) == ("service_connection", "github")
        connect = client.post(
            "/api/mcp-servers/github/connect", json={"identity_mode": IdentityMode.PER_USER}
        )
        assert connect.json()["authorizationUrl"] == (
            "http://druks.test/api/oauth/github/connect?next=/settings/mcp"
        )
        assert client.get("/api/mcp-servers").json()[0]["isEnabled"] is True
        shared = client.post(
            "/api/mcp-servers/github/connect", json={"identity_mode": IdentityMode.SHARED}
        )
        assert shared.status_code == 409


def test_adding_an_unknown_or_existing_server_is_refused(tmp_path, druks_db):
    with _client(tmp_path) as client:
        unknown = client.post("/api/mcp-servers/directory", json={"name": "acme"})
        assert unknown.status_code == 404

        client.post("/api/mcp-servers/directory", json={"name": "sentry"})
        again = client.post("/api/mcp-servers/directory", json={"name": "sentry"})
        assert again.status_code == 409


async def test_removing_a_connected_row_drops_its_grant(tmp_path, druks_db):
    with _client(tmp_path) as client:
        client.post("/api/mcp-servers/directory", json={"name": "grafana"})
        await VaultSecret.connect(
            druks_db, Audience.mcp("grafana"), account_id=None, refresh_token="rt", scopes=[]
        )

        connections = client.get("/api/mcp-servers/grafana/connections").json()
        assert connections == [{"accountUsername": None}]
        assert client.delete("/api/mcp-servers/grafana").status_code == 204

    # An orphan grant would revive as this name's credential on re-add.
    assert not await McpServer.get_for_name(druks_db, "grafana")
    assert not await VaultSecret.list_connections(druks_db, Audience.mcp("grafana"))

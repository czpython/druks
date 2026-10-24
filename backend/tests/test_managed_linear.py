import hashlib
import json
import re
import time
from urllib.parse import parse_qsl, urlparse

import httpx
import jwt as pyjwt
import pytest
from conftest import MANAGER_KEY, bind_ambient_session, connect_service
from druks.accounts.models import Account
from druks.contrib.software_factory import webhooks as webhook_module
from druks.contrib.software_factory.app import SoftwareFactory
from druks.contrib.software_factory.ticketing.enums import TicketStatus
from druks.core.apis.linear import LINEAR_GRAPHQL_URL
from druks.core.services import Linear
from druks.db import db_session
from druks.mcp.exceptions import MissingGrantError
from druks.mcp.models import McpServer
from druks.secrets.datastructures import Audience
from druks.secrets.exceptions import MissingSecretError
from druks.secrets.models import VaultSecret
from druks.services import Service
from druks.testing import asgi_client, configure_app_for_test, make_settings
from druks.workspaces import Workspace

INSTANCE_ID = "6f2c1c1e-9b8e-4b65-9d3c-7a2b1f4e8d10"
AUDIENCE = f"druks:{INSTANCE_ID}"
MANAGER = "https://manager.test"
LINEAR_URL = "https://manager.test/linear"
MANAGER_TOKEN = "manager-secret"
CLIENT_ID = "lin-client"
CONFIGURATION = f"""[manager]
name = "Manager"
jwks_url = "{MANAGER}/.well-known/jwks.json"
issuer = "{MANAGER}"
audience = "{AUDIENCE}"
services = ["linear"]

[urls]
endpoint = "https://instance.test"

[services.linear]
url = "{LINEAR_URL}"
client_id = "{CLIENT_ID}"
"""
FACTS_AFTER_INSTALL = {
    "url": LINEAR_URL,
    "client_id": CLIENT_ID,
    "actor": "Druks",
    "workspace": "Acme",
}
EVENTS = "/_external/linear/events/"
ANSWERS = {
    "facts": {"viewer": {"name": "Druks"}, "organization": {"name": "Acme"}},
    "DruksIssueWorkflowStates": {
        "issue": {
            "id": "issue-1",
            "identifier": "ACME-1",
            "state": {"id": "backlog", "name": "Backlog"},
            "team": {
                "states": {
                    "nodes": [
                        {"id": "backlog", "name": "Backlog"},
                        {"id": "doing", "name": "In Progress"},
                    ]
                }
            },
        }
    },
    "DruksIssueUpdateStatus": {
        "issueUpdate": {
            "success": True,
            "issue": {"identifier": "ACME-1", "state": {"name": "In Progress"}},
        }
    },
    "DruksWorkflowStates": {
        "workflowStates": {
            "nodes": [{"name": "In Progress", "type": "started"}],
            "pageInfo": {"hasNextPage": False, "endCursor": None},
        }
    },
}


def _grant(number: int) -> tuple[int, dict]:
    return 200, {
        "access_token": f"at-{number}",
        "token_type": "Bearer",
        "expires_in": 86399,
        "scope": "read write",
        "refresh_token": f"rt-{number}",
    }


class _Servers:
    def __init__(self) -> None:
        self.token_requests: list[httpx.Request] = []
        self.grants: list = [_grant(1)]
        self.calls: list[tuple[str, str]] = []
        self.refused_tokens: set[str] = set()
        self.is_losing_mutations = False

    @property
    def exchanges(self) -> list[dict]:
        return [dict(parse_qsl(request.content.decode())) for request in self.token_requests]

    async def handle(self, request: httpx.Request) -> httpx.Response:
        if str(request.url) == f"{LINEAR_URL}/oauth/token":
            self.token_requests.append(request)
            answer = self.grants.pop(0)
            if isinstance(answer, Exception):
                raise answer
            status, body = answer
            return httpx.Response(status, request=request, json=body)
        assert str(request.url) == LINEAR_GRAPHQL_URL
        scheme, _, token = request.headers["Authorization"].partition(" ")
        assert scheme == "Bearer"
        named = re.match(r"\s*(?:query|mutation) (\w+)", json.loads(request.content)["query"])
        operation = named.group(1) if named else "facts"
        self.calls.append((token, operation))
        if token in self.refused_tokens:
            refusal = {
                "message": "Authentication required",
                "extensions": {"code": "AUTHENTICATION_ERROR"},
            }
            return httpx.Response(400, request=request, json={"errors": [refusal]})
        if self.is_losing_mutations and operation == "DruksIssueUpdateStatus":
            raise httpx.ReadTimeout("the answer got lost", request=request)
        return httpx.Response(200, request=request, json={"data": ANSWERS[operation]})


@pytest.fixture
async def managed_linear(tmp_path, monkeypatch, druks_db):
    (tmp_path / "druks.toml").write_text(CONFIGURATION)
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "manager.token").write_text(MANAGER_TOKEN)
    monkeypatch.setenv("DRUKS_CONFIG", str(tmp_path / "druks.toml"))
    monkeypatch.setenv("DRUKS_SECRETS_DIR", str(secrets))
    bind_ambient_session(druks_db)
    await Service.sync_configuration(druks_db)
    return make_settings(tmp_path)


@pytest.fixture
async def linear_after_install(managed_linear):
    await connect_service("linear", identity=FACTS_AFTER_INSTALL, secrets={"refresh_token": "rt-1"})


@pytest.fixture
def servers(monkeypatch):
    fake = _Servers()

    async def handle_async_request(transport, request):
        return await fake.handle(request)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", handle_async_request)
    return fake


def _forwarded(body: bytes, delivery_id: str) -> dict[str, str]:
    issued_at = int(time.time())
    signature = pyjwt.encode(
        {
            "iss": MANAGER,
            "aud": AUDIENCE,
            "sub": f"linear:{delivery_id}",
            "body_sha256": hashlib.sha256(body).hexdigest(),
            "iat": issued_at,
            "exp": issued_at + 300,
        },
        MANAGER_KEY,
        algorithm="RS256",
        headers={"kid": "manager-1"},
    )
    return {
        "Content-Type": "application/json",
        "Linear-Delivery": delivery_id,
        "Linear-Event": "Issue",
        "X-Druks-Signature": signature,
    }


def _ready_for_agent() -> bytes:
    body = {
        "action": "update",
        "type": "Issue",
        "organizationId": "org-1",
        "updatedFrom": {"stateId": "backlog"},
        "actor": {"id": "user-1", "type": "user"},
        "data": {
            "identifier": "ACME-1",
            "title": "Ship it",
            "url": "https://linear.app/acme/issue/ACME-1",
            "state": {"name": "Ready for Agent"},
        },
    }
    return json.dumps(body).encode()


async def _get_entry(client: httpx.AsyncClient) -> dict:
    entries = (await client.get("/api/services")).json()
    return next(entry for entry in entries if entry["slug"] == "linear")


async def test_linear_waits_for_its_install_and_keeps_its_grant_across_a_restart(
    managed_linear, servers
):
    row = await Linear.get()
    assert (row.identity, row.secrets) == ({"url": LINEAR_URL, "client_id": CLIENT_ID}, {})
    assert not await Linear.is_connected()
    assert not await SoftwareFactory.get_tracker("linear")

    async with asgi_client(configure_app_for_test(settings=managed_linear)) as client:
        entry = await _get_entry(client)
        assert not entry["connected"] and entry["managed"] and not entry["isOauth"]
        assert entry["installUrl"] == "/api/oauth/linear/connect?install=1"
        # Linear has no personal sign-in: only the install opens its consent page.
        assert (await client.get("/api/oauth/linear/connect")).status_code == 404

        consent = urlparse((await client.get(entry["installUrl"])).headers["location"])
        query = dict(parse_qsl(consent.query))
        assert consent.geturl().startswith("https://linear.app/oauth/authorize?")
        assert (query["actor"], query["scope"]) == ("app", "read,write")
        assert query["state"].startswith(f"{AUDIENCE}.")
        assert query["redirect_uri"] == f"{MANAGER}/linear/oauth/callback"
        assert query["client_id"] == CLIENT_ID
        page = await client.get(
            "/api/oauth/callback", params={"state": query["state"], "code": "c-1"}
        )
        assert page.status_code == 200

        entry = await _get_entry(client)
        assert entry["connected"]
        assert entry["facts"] == FACTS_AFTER_INSTALL

    [exchange] = servers.exchanges
    assert exchange.pop("code_verifier")
    assert exchange == {
        "grant_type": "authorization_code",
        "code": "c-1",
        "redirect_uri": f"{MANAGER}/linear/oauth/callback",
        "actor": "app",
        "scope": "read,write",
        "client_id": CLIENT_ID,
    }
    [token_request] = servers.token_requests
    assert token_request.headers["Authorization"] == f"Bearer {MANAGER_TOKEN}"
    db_session().expunge_all()
    assert not await VaultSecret.list_connections(db_session(), Audience.service("linear"))

    await Service.sync_configuration(db_session())

    row = await Linear.get()
    assert (row.identity, row.secrets) == (FACTS_AFTER_INSTALL, {"refresh_token": "rt-1"})
    assert await Linear.is_connected()
    assert len(servers.token_requests) == 1
    assert servers.calls == [("at-1", "facts")]


async def test_a_refused_install_stores_nothing(managed_linear, servers):
    servers.refused_tokens = {"at-1"}

    async with asgi_client(configure_app_for_test(settings=managed_linear)) as client:
        consent = urlparse(
            (await client.get("/api/oauth/linear/connect?install=1")).headers["location"]
        )
        state = dict(parse_qsl(consent.query))["state"]
        page = await client.get("/api/oauth/callback", params={"state": state, "code": "c-1"})

    assert page.status_code == 400
    db_session().expunge_all()
    row = await Linear.get()
    assert (row.identity, row.secrets) == ({"url": LINEAR_URL, "client_id": CLIENT_ID}, {})


async def test_the_tracker_reads_and_writes_with_the_apps_token(linear_after_install, servers):
    servers.grants = [_grant(2)]

    tracker = await SoftwareFactory.get_tracker("linear")

    async with tracker:
        await tracker.set_status("ACME-1", TicketStatus.IN_PROGRESS)
        servers.is_losing_mutations = True
        with pytest.raises(httpx.ReadTimeout):
            await tracker.set_status("ACME-1", TicketStatus.IN_PROGRESS)

    assert servers.calls == [
        ("at-2", "DruksIssueWorkflowStates"),
        ("at-2", "DruksIssueUpdateStatus"),
        ("at-2", "DruksIssueWorkflowStates"),
        ("at-2", "DruksIssueUpdateStatus"),
    ]
    [token_request] = servers.token_requests
    assert token_request.headers["Authorization"] == f"Bearer {MANAGER_TOKEN}"
    assert servers.exchanges == [
        {"grant_type": "refresh_token", "refresh_token": "rt-1", "client_id": CLIENT_ID}
    ]
    db_session().expunge_all()
    assert (await Linear.get()).secrets == {"refresh_token": "rt-2"}


async def test_a_lost_refresh_answer_goes_again(linear_after_install, servers):
    servers.grants = [httpx.ConnectError("the answer got lost"), _grant(2)]

    tracker = await SoftwareFactory.get_tracker("linear")
    async with tracker:
        choices = await tracker.list_status_choices()

    assert choices == [{"value": "In Progress", "label": "In Progress", "group": "started"}]
    assert [exchange["refresh_token"] for exchange in servers.exchanges] == ["rt-1", "rt-1"]
    assert servers.calls == [("at-2", "DruksWorkflowStates")]
    db_session().expunge_all()
    assert (await Linear.get()).secrets == {"refresh_token": "rt-2"}


@pytest.mark.parametrize(
    "answer",
    [
        (400, {"error": "invalid_grant"}),
        (403, {"error": "access_denied", "error_description": "The installation is revoked."}),
    ],
)
async def test_a_revoked_grant_asks_for_the_install_again(
    linear_after_install, managed_linear, servers, answer
):
    servers.grants = [answer]

    assert not await SoftwareFactory.get_tracker("linear")

    db_session().expunge_all()
    row = await Linear.get()
    assert (row.identity, row.secrets) == (FACTS_AFTER_INSTALL, {})
    with pytest.raises(MissingSecretError):
        await row.issue_token("")
    async with asgi_client(configure_app_for_test(settings=managed_linear)) as client:
        entry = await _get_entry(client)
    assert not entry["connected"]
    assert entry["installUrl"] == "/api/oauth/linear/connect?install=1"


async def test_the_default_account_sends_the_apps_token_to_linears_mcp_server(
    managed_linear, servers, druks_db
):
    default_account = await Account.get_or_create(druks_db, "default@example.com")
    await McpServer.create(druks_db, name="linear", url="https://mcp.linear.app/mcp", is_oauth=True)

    with pytest.raises(MissingGrantError):
        await Workspace.get_all_mcp_servers(druks_db, None, default_account.id)

    row = await connect_service(
        "linear", identity=FACTS_AFTER_INSTALL, secrets={"refresh_token": "rt-1"}
    )
    [server], [ref] = await Workspace.get_all_mcp_servers(druks_db, None, default_account.id)

    assert (ref.secret_id, ref.name) == (row.id, server.bearer_token_env_var.lower())
    token, expires_at = await row.issue_token(ref.resource, name=ref.name)
    assert token == "at-1" and expires_at


async def test_a_forwarded_event_reaches_the_factory(managed_linear, manager_keys, monkeypatch):
    published = []

    async def capture(event_type, **kwargs):
        published.append((event_type, kwargs["payload"]["identifier"]))

    monkeypatch.setattr(webhook_module, "publish", capture)
    await connect_service("linear", identity=FACTS_AFTER_INSTALL, secrets={"refresh_token": "rt-1"})
    body = _ready_for_agent()

    async with asgi_client(configure_app_for_test(settings=managed_linear)) as client:
        response = await client.post(EVENTS, content=body, headers=_forwarded(body, "d-1"))

    assert response.json() == {"accepted": True}
    assert published == [("ticket.transitioned", "ACME-1")]

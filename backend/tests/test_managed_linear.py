import hashlib
import json
import re
import time
from urllib.parse import parse_qsl, urlparse

import httpx
import jwt as pyjwt
import pytest
from conftest import PORTAL_KEY, bind_ambient_session, connect_service
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
from druks.services import OauthRefreshError, Service
from druks.testing import asgi_client, configure_app_for_test, make_settings
from druks.workspaces import Workspace

INSTANCE_ID = "6f2c1c1e-9b8e-4b65-9d3c-7a2b1f4e8d10"
AUDIENCE = f"druks:{INSTANCE_ID}"
PORTAL = "https://portal.test"
CARD_URL = "https://manager.test/linear"
MANAGER_TOKEN = "manager-secret"
CLIENT_ID = "lin-client"
CONFIGURATION = f"""[manager]
name = "Manager"
jwks_url = "{PORTAL}/.well-known/jwks.json"
issuer = "{PORTAL}"
audience = "{AUDIENCE}"
services = ["linear"]

[urls]
endpoint = "https://instance.test"

[services.linear]
url = "{CARD_URL}"
client_id = "{CLIENT_ID}"
"""
# The card after the install: the table's fields and what the app's token proves.
FACTS_AFTER_INSTALL = {
    "url": CARD_URL,
    "client_id": CLIENT_ID,
    "actor": "Druks",
    "workspace": "Acme",
    "workspace_id": "org-1",
}
EVENTS = "/_external/linear/events/"
# What Linear answers to each operation.
ANSWERS = {
    "facts": {"viewer": {"name": "Druks"}, "organization": {"id": "org-1", "name": "Acme"}},
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
    """The token endpoint's answer that issues the grant's ``number``th tokens."""
    return 200, {
        "access_token": f"at-{number}",
        "token_type": "Bearer",
        "expires_in": 86399,
        "scope": "read write",
        "refresh_token": f"rt-{number}",
    }


class _Cloud:
    """The manager and Linear's API, answering at the transport. The manager answers
    each token request with the next of ``grants``, where an exception is an answer that
    got lost. Linear refuses the access tokens in ``refused_tokens``, and loses the answer
    to a mutation while ``is_losing_mutations``."""

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
        if str(request.url) == f"{CARD_URL}/oauth/token":
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
    """The settings of a Druks whose Linear app the manager owns, its card synced."""
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
async def card_after_install(managed_linear):
    await connect_service("linear", identity=FACTS_AFTER_INSTALL, secrets={"refresh_token": "rt-1"})


@pytest.fixture
def cloud(monkeypatch):
    fake = _Cloud()

    async def handle_async_request(transport, request):
        return await fake.handle(request)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", handle_async_request)
    return fake


def _forwarded(body: bytes, delivery_id: str) -> dict[str, str]:
    """The headers of an event that the manager forwards, signed with its key."""
    issued_at = int(time.time())
    signature = pyjwt.encode(
        {
            "iss": PORTAL,
            "aud": AUDIENCE,
            "sub": f"linear:{delivery_id}",
            "body_sha256": hashlib.sha256(body).hexdigest(),
            "iat": issued_at,
            "exp": issued_at + 300,
        },
        PORTAL_KEY,
        algorithm="RS256",
        headers={"kid": "portal-1"},
    )
    return {
        "Content-Type": "application/json",
        "Linear-Delivery": delivery_id,
        "Linear-Event": "Issue",
        "X-Druks-Signature": signature,
    }


def _ready_for_agent(organization: str = "org-1") -> bytes:
    """Someone moves ACME-1 to the trigger status, in ``organization``."""
    body = {
        "action": "update",
        "type": "Issue",
        "organizationId": organization,
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


async def _get_card(client: httpx.AsyncClient) -> dict:
    """The Linear card, as the Services page reads it."""
    entries = (await client.get("/api/services")).json()
    return next(entry for entry in entries if entry["slug"] == "linear")


async def test_the_card_waits_for_its_install_and_keeps_its_grant_across_a_restart(
    managed_linear, cloud
):
    card = await Linear.get()
    assert (card.identity, card.secrets) == ({"url": CARD_URL, "client_id": CLIENT_ID}, {})
    assert not await Linear.is_connected()
    assert not await SoftwareFactory.get_tracker("linear")

    async with asgi_client(configure_app_for_test(settings=managed_linear)) as client:
        entry = await _get_card(client)
        assert not entry["connected"] and entry["managed"] and not entry["isOauth"]
        assert entry["installUrl"] == "/api/oauth/linear/connect?install=1"
        # Linear has no personal sign-in: only the install opens its consent page.
        assert (await client.get("/api/oauth/linear/connect")).status_code == 404

        consent = urlparse((await client.get(entry["installUrl"])).headers["location"])
        query = dict(parse_qsl(consent.query))
        assert consent.geturl().startswith("https://linear.app/oauth/authorize?")
        assert (query["actor"], query["scope"]) == ("app", "read,write")
        assert query["state"].startswith(f"{AUDIENCE}.")
        assert query["redirect_uri"] == f"{PORTAL}/linear/oauth/callback"
        assert query["client_id"] == CLIENT_ID
        page = await client.get(
            "/api/oauth/callback", params={"state": query["state"], "code": "c-1"}
        )
        assert page.status_code == 200

        entry = await _get_card(client)
        assert entry["connected"]
        assert entry["facts"] == FACTS_AFTER_INSTALL

    [exchange] = cloud.exchanges
    assert exchange.pop("code_verifier")
    assert exchange == {
        "grant_type": "authorization_code",
        "code": "c-1",
        "redirect_uri": f"{PORTAL}/linear/oauth/callback",
        "actor": "app",
        "scope": "read,write",
        "client_id": CLIENT_ID,
    }
    [token_request] = cloud.token_requests
    assert token_request.headers["Authorization"] == f"Bearer {MANAGER_TOKEN}"
    db_session().expunge_all()
    assert not await VaultSecret.list_connections(db_session(), Audience.service("linear"))

    await Service.sync_configuration(db_session())

    card = await Linear.get()
    assert (card.identity, card.secrets) == (FACTS_AFTER_INSTALL, {"refresh_token": "rt-1"})
    assert await Linear.is_connected()
    assert len(cloud.token_requests) == 1
    assert cloud.calls == [("at-1", "facts")]


async def test_a_refused_install_stores_nothing(managed_linear, cloud):
    cloud.refused_tokens = {"at-1"}

    async with asgi_client(configure_app_for_test(settings=managed_linear)) as client:
        consent = urlparse(
            (await client.get("/api/oauth/linear/connect?install=1")).headers["location"]
        )
        state = dict(parse_qsl(consent.query))["state"]
        page = await client.get("/api/oauth/callback", params={"state": state, "code": "c-1"})

    assert page.status_code == 400
    db_session().expunge_all()
    card = await Linear.get()
    assert (card.identity, card.secrets) == ({"url": CARD_URL, "client_id": CLIENT_ID}, {})


async def test_the_tracker_reads_and_writes_with_the_apps_token(card_after_install, cloud):
    """The tracker fetches no token before its first request. The manager rotates the
    grant, and a mutation whose answer got lost goes out once."""
    cloud.grants = [_grant(2)]

    tracker = await SoftwareFactory.get_tracker("linear")

    assert not cloud.token_requests
    async with tracker:
        await tracker.set_status("ACME-1", TicketStatus.IN_PROGRESS)
        cloud.is_losing_mutations = True
        with pytest.raises(httpx.ReadTimeout):
            await tracker.set_status("ACME-1", TicketStatus.IN_PROGRESS)

    assert cloud.calls == [
        ("at-2", "DruksIssueWorkflowStates"),
        ("at-2", "DruksIssueUpdateStatus"),
        ("at-2", "DruksIssueWorkflowStates"),
        ("at-2", "DruksIssueUpdateStatus"),
    ]
    [token_request] = cloud.token_requests
    assert token_request.headers["Authorization"] == f"Bearer {MANAGER_TOKEN}"
    assert cloud.exchanges == [
        {"grant_type": "refresh_token", "refresh_token": "rt-1", "client_id": CLIENT_ID}
    ]
    db_session().expunge_all()
    assert (await Linear.get()).secrets == {"refresh_token": "rt-2"}


async def test_a_refused_token_refreshes_once_and_a_lost_refresh_answer_goes_again(
    card_after_install, cloud
):
    """Linear refuses a token: the tracker refreshes once and sends the request once more.
    A refresh whose answer got lost goes again with the same refresh token, which Linear
    answers in its grace window. A token that Linear refuses after the refresh ends the
    call."""
    cloud.grants = [_grant(2), httpx.ConnectError("the answer got lost"), _grant(3), _grant(4)]
    cloud.refused_tokens = {"at-2"}
    tracker = await SoftwareFactory.get_tracker("linear")

    async with tracker:
        choices = await tracker.list_status_choices()
        cloud.refused_tokens = {"at-3", "at-4"}
        with pytest.raises(httpx.HTTPStatusError):
            await tracker.list_status_choices()

    assert choices == [{"value": "In Progress", "label": "In Progress", "group": "started"}]
    assert [exchange["refresh_token"] for exchange in cloud.exchanges] == [
        "rt-1",
        "rt-2",
        "rt-2",
        "rt-3",
    ]
    assert cloud.calls == [
        ("at-2", "DruksWorkflowStates"),
        ("at-3", "DruksWorkflowStates"),
        ("at-3", "DruksWorkflowStates"),
        ("at-4", "DruksWorkflowStates"),
    ]
    db_session().expunge_all()
    assert (await Linear.get()).secrets == {"refresh_token": "rt-4"}


@pytest.mark.parametrize(
    "answer",
    [
        (400, {"error": "invalid_grant"}),
        (403, {"error": "access_denied", "error_description": "The installation is revoked."}),
    ],
)
async def test_a_revoked_grant_asks_for_the_install_again(
    card_after_install, managed_linear, cloud, answer
):
    """Linear refuses the grant, or the manager refuses a revoked installation: the card
    keeps its configuration and facts, loses the grant, and offers the install again."""
    cloud.grants = [answer]
    tracker = await SoftwareFactory.get_tracker("linear")

    async with tracker:
        for _ in range(2):
            with pytest.raises(OauthRefreshError, match="install the app again"):
                await tracker.list_status_choices()

    db_session().expunge_all()
    card = await Linear.get()
    assert (card.identity, card.secrets) == (FACTS_AFTER_INSTALL, {})
    assert not await SoftwareFactory.get_tracker("linear")
    with pytest.raises(MissingSecretError):
        await card.issue_token("org-1")
    async with asgi_client(configure_app_for_test(settings=managed_linear)) as client:
        entry = await _get_card(client)
    assert not entry["connected"]
    assert entry["installUrl"] == "/api/oauth/linear/connect?install=1"


async def test_the_default_account_sends_the_apps_token_to_linears_mcp_server(
    managed_linear, cloud, druks_db
):
    default_account = await Account.get_or_create(druks_db, "default@example.com")
    await McpServer.create(druks_db, name="linear", url="https://mcp.linear.app/mcp", is_oauth=True)

    with pytest.raises(MissingGrantError):
        await Workspace.get_all_mcp_servers(druks_db, None, default_account.id)

    card = await connect_service(
        "linear", identity=FACTS_AFTER_INSTALL, secrets={"refresh_token": "rt-1"}
    )
    [server], [ref] = await Workspace.get_all_mcp_servers(druks_db, None, default_account.id)

    # The box fetches the card's current access token in its workspace, and fetches again
    # before the token expires.
    assert (ref.secret_id, ref.name, ref.resource) == (
        card.id,
        server.bearer_token_env_var.lower(),
        "org-1",
    )
    token, expires_at = await card.issue_token(ref.resource, name=ref.name)
    assert token == "at-1" and expires_at


async def test_forwarded_events_reach_the_factory_only_from_its_workspace(
    managed_linear, portal_keys, monkeypatch
):
    published = []

    async def capture(event_type, **kwargs):
        published.append((event_type, kwargs["payload"]["identifier"]))

    monkeypatch.setattr(webhook_module, "publish", capture)
    early = _ready_for_agent()

    async with asgi_client(configure_app_for_test(settings=managed_linear)) as client:
        before_install = await client.post(EVENTS, content=early, headers=_forwarded(early, "d-0"))
        await connect_service(
            "linear", identity=FACTS_AFTER_INSTALL, secrets={"refresh_token": "rt-1"}
        )
        comment = {"action": "create", "type": "Comment", "organizationId": "org-2"}
        bodies = (
            ("d-1", _ready_for_agent()),
            ("d-2", _ready_for_agent("org-2")),
            ("d-3", json.dumps(comment).encode()),
        )
        responses = []
        for delivery_id, body in bodies:
            responses.append(
                await client.post(EVENTS, content=body, headers=_forwarded(body, delivery_id))
            )

    assert before_install.json() == {"accepted": False}
    assert [response.json() for response in responses] == [
        {"accepted": True},
        {"accepted": False},
        {"accepted": False},
    ]
    assert published == [("ticket.transitioned", "ACME-1")]

import hashlib
import json
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from urllib.parse import parse_qsl, urlparse

import druks.core.apis.github as github_api
import httpx
import jwt as pyjwt
import pytest
from conftest import bind_ambient_session
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from druks.accounts import jwt as assertion
from druks.core.apis.exceptions import GitHubAppNotInstalledError
from druks.core.apis.github import GITHUB_API_URL
from druks.core.services import Github
from druks.db import db_session
from druks.redis import get_client
from druks.secrets.datastructures import Audience
from druks.secrets.models import VaultSecret
from druks.services import Connection, Service
from druks.testing import asgi_client, configure_app_for_test, make_settings
from fastmcp.server.auth.providers.jwt import JWTVerifier
from githubkit import AppAuthStrategy, TokenAuthStrategy
from githubkit.exception import RequestFailed

INSTANCE_ID = "6f2c1c1e-9b8e-4b65-9d3c-7a2b1f4e8d10"
PORTAL = "https://portal.test"
RELAY = f"http://cloud.test/_instance/{INSTANCE_ID}/github"
MANAGER_TOKEN = "manager-secret"
CONFIGURATION = f"""[manager]
name = "Druks Cloud"
jwks_url = "{PORTAL}/.well-known/jwks.json"
issuer = "{PORTAL}"
audience = "{INSTANCE_ID}"
services = ["github"]

[urls]
endpoint = "https://instance.test"

[services.github]
url = "{RELAY}"
app_id = "4242"
client_id = "Iv1.abc"
slug = "druks"
"""
# This Druks's installations, by account login.
INSTALLATIONS = {"acme": 11, "paulo": 22}
EVENTS = "/_external/github/events/"

_PORTAL_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_JWKS = {
    "keys": [
        {
            **pyjwt.algorithms.RSAAlgorithm.to_jwk(_PORTAL_KEY.public_key(), as_dict=True),
            "kid": "portal-1",
        }
    ]
}
# The instance's own key signs the App's calls at the relay.
_INSTANCE_PEM = (
    rsa.generate_private_key(public_exponent=65537, key_size=2048)
    .private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    .decode()
    .strip()
)
INSTANCE_KEY = AppAuthStrategy("4242", _INSTANCE_PEM)


def _refused(status_code: int) -> RequestFailed:
    """githubkit's failure for an answer with ``status_code``."""
    error = RequestFailed.__new__(RequestFailed)
    error.response = SimpleNamespace(status_code=status_code)  # type: ignore[assignment]
    return error


class _GitHubKit:
    """githubkit as the tests see it: the credential and base URL of every client it made, and
    the calls they were asked for. The App's directory answers for this Druks's installations;
    repository calls answer with ``statuses``, 204 once those run out."""

    def __init__(self) -> None:
        self.clients: list[tuple[object, str]] = []
        self.calls: list[str] = []
        self.minted = 0
        self.statuses: list[int] = []

    def __call__(self, auth: object = None, *, base_url: str = GITHUB_API_URL) -> SimpleNamespace:
        self.clients.append((auth, base_url))
        apps = SimpleNamespace(
            async_get_repo_installation=self._installation_of,
            async_create_installation_access_token=self._mint,
            async_list_installations=self._installations,
        )
        git = SimpleNamespace(async_delete_ref=self._delete_ref)
        return SimpleNamespace(rest=SimpleNamespace(apps=apps, git=git), __aexit__=self._close)

    async def _installation_of(self, owner: str, name: str) -> SimpleNamespace:
        self.calls.append(f"GET repos/{owner}/{name}/installation")
        if owner not in INSTALLATIONS:
            raise _refused(404)
        return SimpleNamespace(parsed_data=SimpleNamespace(id=INSTALLATIONS[owner]))

    async def _mint(self, installation_id: int) -> SimpleNamespace:
        self.calls.append(f"POST app/installations/{installation_id}/access_tokens")
        self.minted += 1
        expires_at = (datetime.now(UTC) + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        token = SimpleNamespace(token=f"ghs_{installation_id}_{self.minted}", expires_at=expires_at)
        return SimpleNamespace(parsed_data=token)

    async def _installations(self, per_page: int, page: int) -> SimpleNamespace:
        self.calls.append("GET app/installations")
        rows = [SimpleNamespace(account=SimpleNamespace(login=login)) for login in INSTALLATIONS]
        return SimpleNamespace(parsed_data=rows)

    async def _delete_ref(self, owner: str, name: str, ref: str) -> None:
        self.calls.append(f"DELETE repos/{owner}/{name}/git/{ref}")
        status = self.statuses.pop(0) if self.statuses else 204
        if status >= 400:
            raise _refused(status)

    async def _close(self, *args: object) -> None:
        pass


class _Relay:
    """The relay's token exchange, answering at the transport: what it was asked, and the
    ``status`` and ``answer`` it gives."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.exchanges: list[dict] = []
        self.status = 200
        self.answer = {"access_token": "ghu-1", "refresh_token": "ghr-1", "expires_in": 28800}

    async def handle(self, request: httpx.Request) -> httpx.Response:
        assert str(request.url) == f"{RELAY}/oauth/token"
        self.requests.append(request)
        self.exchanges.append(dict(parse_qsl(request.content.decode())))
        return httpx.Response(self.status, request=request, json=self.answer)


@pytest.fixture
async def managed_github(tmp_path, monkeypatch, druks_db):
    """The settings of a Druks with a managed GitHub App, its card synced."""
    (tmp_path / "druks.toml").write_text(CONFIGURATION)
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "manager.token").write_text(MANAGER_TOKEN)
    (secrets / "services.github.private_key").write_text(_INSTANCE_PEM)
    monkeypatch.setenv("DRUKS_CONFIG", str(tmp_path / "druks.toml"))
    monkeypatch.setenv("DRUKS_SECRETS_DIR", str(secrets))
    bind_ambient_session(druks_db)
    await Service.sync_configuration(druks_db)
    return make_settings(tmp_path)


@pytest.fixture
def githubkit(monkeypatch):
    fake = _GitHubKit()
    monkeypatch.setattr(github_api, "GitHub", fake)
    return fake


@pytest.fixture
def relay(monkeypatch):
    fake = _Relay()

    async def handle_async_request(transport, request):
        return await fake.handle(request)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", handle_async_request)
    return fake


@pytest.fixture
def portal_keys(monkeypatch):
    assertion._verifier.cache_clear()

    async def fetch_jwks(self):
        return _JWKS

    monkeypatch.setattr(JWTVerifier, "_fetch_jwks", fetch_jwks)
    yield
    assertion._verifier.cache_clear()


@pytest.fixture
def github_identity(monkeypatch):
    async def identity(cls, access_token: str) -> dict:
        return {"authority": "https://github.com", "subject": "1", "login": "paulo"}

    monkeypatch.setattr(Github, "get_identity", classmethod(identity))


def _signature(body: bytes, delivery_id: str = "d-1", **claims) -> str:
    issued_at = int(time.time())
    return pyjwt.encode(
        {
            "iss": PORTAL,
            "aud": INSTANCE_ID,
            "sub": f"github:{delivery_id}",
            "body_sha256": hashlib.sha256(body).hexdigest(),
            "iat": issued_at,
            "exp": issued_at + 300,
            **claims,
        },
        _PORTAL_KEY,
        algorithm="RS256",
        headers={"kid": "portal-1"},
    )


async def test_the_managed_card_holds_the_apps_public_identity_and_the_instance_key(
    managed_github,
):
    card = await Github.get()
    assert card.identity == {
        "url": RELAY,
        "app_id": "4242",
        "client_id": "Iv1.abc",
        "slug": "druks",
    }
    assert card.secrets == {"private_key": _INSTANCE_PEM}

    async with asgi_client(configure_app_for_test(settings=managed_github)) as client:
        response = await client.get("/api/services")

    [entry] = [entry for entry in response.json() if entry["slug"] == "github"]
    assert entry["connected"] and entry["managed"]
    assert entry["managedBy"] == "Druks Cloud"
    assert entry["facts"] == card.identity
    assert MANAGER_TOKEN not in response.text
    assert _INSTANCE_PEM not in response.text


async def test_app_calls_go_to_the_relay_with_the_instance_key_and_repo_calls_to_github(
    managed_github, githubkit
):
    client = await Github.get_client()

    assert await client.delete_branch("acme/app", "feature")
    token, expires_at = await Github.issue_token("acme/app")
    assert await client.delete_branch("paulo/site", "feature")
    with pytest.raises(GitHubAppNotInstalledError, match="other/repo"):
        await client.delete_branch("other/repo", "feature")

    assert token == "ghs_11_1"
    assert expires_at > datetime.now(UTC)
    assert githubkit.clients == [
        (INSTANCE_KEY, RELAY),
        (TokenAuthStrategy("ghs_11_1"), GITHUB_API_URL),
        (INSTANCE_KEY, RELAY),
        (TokenAuthStrategy("ghs_22_2"), GITHUB_API_URL),
    ]
    assert githubkit.calls == [
        "GET repos/acme/app/installation",
        "POST app/installations/11/access_tokens",
        "DELETE repos/acme/app/git/heads/feature",
        "GET repos/acme/app/installation",
        "GET repos/paulo/site/installation",
        "POST app/installations/22/access_tokens",
        "DELETE repos/paulo/site/git/heads/feature",
        "GET repos/other/repo/installation",
    ]


async def test_a_token_is_kept_in_redis_until_it_expires(managed_github, githubkit):
    await (await Github.get_client()).delete_branch("acme/app", "first")
    redis = get_client()
    # GitHub's hour, less the skew every cached token keeps.
    assert 3530 <= await redis.ttl("github:installation_token:11") <= 3540
    await (await Github.get_client()).delete_branch("acme/app", "second")
    assert githubkit.minted == 1

    await redis.delete("github:installation_token:11")
    await (await Github.get_client()).delete_branch("acme/app", "third")

    assert githubkit.minted == 2
    assert [auth for auth, base_url in githubkit.clients if base_url == GITHUB_API_URL] == [
        TokenAuthStrategy("ghs_11_1"),
        TokenAuthStrategy("ghs_11_1"),
        TokenAuthStrategy("ghs_11_2"),
    ]


async def test_a_rejected_token_is_minted_once_more(managed_github, githubkit):
    client = await Github.get_client()

    githubkit.statuses = [401, 204]
    assert await client.delete_branch("acme/app", "first")
    assert githubkit.minted == 2

    githubkit.statuses = [401, 401]
    with pytest.raises(RequestFailed) as rejected:
        await client.delete_branch("acme/app", "second")
    assert rejected.value.response.status_code == 401
    assert githubkit.minted == 3


async def test_install_binds_at_the_manager_and_links_the_installer(
    managed_github, githubkit, relay, github_identity
):
    async with asgi_client(configure_app_for_test(settings=managed_github)) as client:
        response = await client.get("/api/oauth/github/connect", params={"install": "1"})
        assert response.status_code == 307
        consent = urlparse(response.headers["location"])
        query = dict(parse_qsl(consent.query))
        assert consent.geturl().startswith("https://github.com/apps/druks/installations/new?")
        assert query["state"].startswith(f"{INSTANCE_ID}.")
        assert query["redirect_uri"] == f"{PORTAL}/github/oauth/callback"
        assert query["client_id"] == "Iv1.abc"

        page = await client.get(
            "/api/oauth/callback",
            params={"state": query["state"], "code": "c-1", "installation_id": "11"},
        )
        assert page.status_code == 200

    [exchange] = relay.exchanges
    assert (
        exchange.items()
        >= {
            "grant_type": "authorization_code",
            "code": "c-1",
            "redirect_uri": f"{PORTAL}/github/oauth/callback",
            "installation_id": "11",
            "client_id": "Iv1.abc",
        }.items()
    )
    assert "client_secret" not in exchange
    [token_request] = relay.requests
    assert token_request.headers["Authorization"] == f"Bearer {MANAGER_TOKEN}"
    assert githubkit.calls == ["GET app/installations"]
    db_session().expunge_all()
    assert (await Github.get()).identity["installations"] == ["acme", "paulo"]
    [connection] = await VaultSecret.list_connections(db_session(), Audience.service("github"))
    assert connection.account.username == "op@example.com"
    assert connection.secrets == {"refresh_token": "ghr-1"}
    assert connection.identity["login"] == "paulo"


async def test_a_personal_link_and_its_refresh_go_through_the_relay(
    managed_github, relay, github_identity
):
    async with asgi_client(configure_app_for_test(settings=managed_github)) as client:
        response = await client.get("/api/oauth/github/connect")
        consent = urlparse(response.headers["location"])
        query = dict(parse_qsl(consent.query))
        assert consent.geturl().startswith("https://github.com/login/oauth/authorize?")
        assert query["state"].startswith(f"{INSTANCE_ID}.")
        page = await client.get(
            "/api/oauth/callback", params={"state": query["state"], "code": "c-2"}
        )
        assert page.status_code == 200

    db_session().expunge_all()
    [connection] = await VaultSecret.list_connections(db_session(), Audience.service("github"))
    assert "installations" not in (await Github.get()).identity
    assert "installation_id" not in relay.exchanges[0]

    relay.answer = {"access_token": "ghu-2", "refresh_token": "ghr-2", "expires_in": 3600}
    assert await Connection(Github, connection).get_access_token() == "ghu-2"
    assert relay.exchanges[1] == {
        "grant_type": "refresh_token",
        "refresh_token": "ghr-1",
        "client_id": "Iv1.abc",
    }
    [_, refresh] = relay.requests
    assert refresh.headers["Authorization"] == f"Bearer {MANAGER_TOKEN}"
    assert connection.account.username == "op@example.com"


async def test_a_refused_exchange_links_nothing(managed_github, relay):
    relay.status = 403
    relay.answer = {"error": "access_denied"}

    async with asgi_client(configure_app_for_test(settings=managed_github)) as client:
        response = await client.get("/api/oauth/github/connect", params={"install": "1"})
        state = dict(parse_qsl(urlparse(response.headers["location"]).query))["state"]
        page = await client.get(
            "/api/oauth/callback",
            params={"state": state, "code": "c-3", "installation_id": "99"},
        )

    assert page.status_code == 400
    db_session().expunge_all()
    assert not await VaultSecret.list_connections(db_session(), Audience.service("github"))
    assert "installations" not in (await Github.get()).identity


async def test_forwarded_events_carry_the_managers_signature(managed_github, portal_keys):
    body = json.dumps({"action": "labeled", "repository": {"full_name": "acme/app"}}).encode()
    headers = {"X-GitHub-Event": "pull_request", "Content-Type": "application/json"}

    async with asgi_client(configure_app_for_test(settings=managed_github)) as client:
        accepted = await client.post(
            EVENTS,
            content=body,
            headers={**headers, "X-GitHub-Delivery": "d-1", "X-Druks-Signature": _signature(body)},
        )
        assert accepted.status_code == 200
        assert accepted.json() == {"accepted": True, "handled": False}

        retried = await client.post(
            EVENTS,
            content=body,
            headers={**headers, "X-GitHub-Delivery": "d-1", "X-Druks-Signature": _signature(body)},
        )
        assert retried.json() == {"accepted": False, "duplicate": True}

        late = await client.post(
            EVENTS,
            content=body,
            headers={
                **headers,
                "X-GitHub-Delivery": "d-2",
                "X-Druks-Signature": _signature(body, "d-2"),
            },
        )
        assert late.json() == {"accepted": True, "handled": False}

        for signature in (
            _signature(body, "d-3", aud="another-instance"),
            _signature(b'{"action":"other"}', "d-3"),
            _signature(body, "d-3", exp=int(time.time()) - 1),
            _signature(body, "d-9"),
            _signature(body, "d-3", body_sha256=None),
            "",
        ):
            refused = await client.post(
                EVENTS,
                content=body,
                headers={**headers, "X-GitHub-Delivery": "d-3", "X-Druks-Signature": signature},
            )
            assert refused.status_code == 401

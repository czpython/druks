import json
import time
from urllib.parse import parse_qsl, urlparse

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from druks.mcp import oauth
from druks.mcp.enums import IdentityMode
from druks.mcp.exceptions import OauthConnectError
from druks.redis import get_client

ISSUER = "https://mcp.linear.app"
SERVER = f"{ISSUER}/mcp"
NAME = "our_connection"
PRIVATE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def provider(monkeypatch, request):
    """Public Linear and Atlassian metadata layouts with local test keys and claims."""
    oauth_metadata = {
        "issuer": ISSUER,
        "authorization_endpoint": f"{ISSUER}/authorize",
        "token_endpoint": f"{ISSUER}/token",
        "registration_endpoint": f"{ISSUER}/register",
        "scopes_supported": ["read", "write", "openid", "email"],
        "code_challenge_methods_supported": ["S256"],
    }
    oidc_metadata = {
        **oauth_metadata,
        "jwks_uri": f"{ISSUER}/.well-known/jwks.json",
        "id_token_signing_alg_values_supported": ["RS256"],
        "subject_types_supported": ["public"],
        "claims_supported": ["sub", "iss", "aud", "exp", "iat", "email", "email_verified"],
    }
    server_url = SERVER
    resource_scopes = ["read", "write"]
    if getattr(request, "param", "linear") == "atlassian":
        # Public metadata: auth.atlassian.com exposes a scoped OAuth issuer and root OpenID issuer.
        server_url = "https://mcp.atlassian.com/v2/mcp"
        origin = "https://auth.atlassian.com"
        oauth_metadata = {
            "issuer": f"{origin}/scoped-client",
            "authorization_endpoint": f"{origin}/authorize",
            "token_endpoint": f"{origin}/oauth/token",
            "registration_endpoint": f"{origin}/scoped-client/dcr/register",
            "jwks_uri": f"{origin}/.well-known/jwks.json",
            "code_challenge_methods_supported": ["S256"],
        }
        oidc_metadata = {
            "issuer": origin,
            "authorization_endpoint": oauth_metadata["authorization_endpoint"],
            "token_endpoint": oauth_metadata["token_endpoint"],
            "jwks_uri": oauth_metadata["jwks_uri"],
            "userinfo_endpoint": f"{origin}/userinfo",
            "scopes_supported": ["openid", "profile", "email", "offline_access"],
            "id_token_signing_alg_values_supported": ["HS256", "RS256"],
        }
        resource_scopes = [
            "read:me",
            "read:account",
            "offline_access",
            "email",
            "read:jira:agent-interface",
        ]
    state = {
        "server": server_url,
        "oauth": oauth_metadata,
        "openid": oidc_metadata,
        "resource": {
            "resource": server_url,
            "authorization_servers": [oauth_metadata["issuer"]],
            "scopes_supported": resource_scopes,
        },
        "claims": {
            "iss": oidc_metadata["issuer"],
            "aud": "test-client",
            "sub": "provider-user-1",
            "email": "operator@example.test",
            "email_verified": True,
            "exp": int(time.time()) + 600,
            "iat": int(time.time()),
        },
        "requests": [],
    }

    resource = urlparse(server_url)
    issuer = urlparse(oauth_metadata["issuer"])
    documents = {
        (
            f"{resource.scheme}://{resource.netloc}"
            f"/.well-known/oauth-protected-resource{resource.path}"
        ): "resource",
        (
            f"{issuer.scheme}://{issuer.netloc}/.well-known/oauth-authorization-server{issuer.path}"
        ): "oauth",
        f"{oidc_metadata['issuer']}/.well-known/openid-configuration": "openid",
    }

    def handler(request):
        state["requests"].append(request)
        url = str(request.url)
        if url in documents:
            document = state[documents[url]]
            return httpx.Response(200, json=document) if document else httpx.Response(404)
        if url == oauth_metadata["registration_endpoint"]:
            return httpx.Response(201, json={"client_id": "test-client"})
        if url == oauth_metadata["token_endpoint"]:
            return httpx.Response(
                200,
                json={
                    "access_token": "test-access-token",
                    "refresh_token": "test-refresh-token",
                    "expires_in": 3600,
                    "resource": server_url,
                    "token_type": "Bearer",
                    "id_token": jwt.encode(
                        state["claims"], PRIVATE_KEY, algorithm="RS256", headers={"kid": "test-key"}
                    ),
                },
            )
        return httpx.Response(404)

    def client():
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    monkeypatch.setattr(oauth, "_http", client)
    monkeypatch.setattr("druks.services.oauth._http", client)
    return state


@pytest.mark.parametrize("provider", ["linear", "atlassian"], indirect=True)
async def test_connect_resolves_standard_identity(provider, druks_db):
    """Both public metadata layouts validate against their OpenID issuer."""
    url = await oauth.begin_connect(
        NAME,
        provider["server"],
        "https://druks.example.test",
        account_id=None,
        identity_mode=IdentityMode.SHARED,
    )
    params = dict(parse_qsl(urlparse(url).query))
    requested = params["scope"].split()
    assert set(requested) == set(provider["resource"]["scopes_supported"]) | {
        scope
        for scope in ("openid", "email", "profile")
        if scope in provider["openid"]["scopes_supported"]
    }
    assert len(requested) == len(set(requested))
    registration = next(
        request
        for request in provider["requests"]
        if str(request.url) == provider["oauth"]["registration_endpoint"]
    )
    assert json.loads(registration.content)["scope"] == params["scope"]
    pending = json.loads(await get_client().get(f"oauth:connect:{params['state']}"))
    assert pending["issuer"] == provider["openid"]["issuer"]
    assert pending["token_endpoint"] == provider["oauth"]["token_endpoint"]
    provider["claims"]["nonce"] = params["nonce"]

    await oauth.complete_connect(state=params["state"], code="test-authorization-code")

    grant = await oauth.get_connection(NAME, None)
    assert grant.identity_status == "resolved"
    assert grant.identity == {
        "authority": provider["openid"]["issuer"],
        "subject": "provider-user-1",
        "source": "id_token",
        "email": "operator@example.test",
        "email_verified": True,
    }
    assert grant.scopes == requested


async def test_openid_only_issuer_can_register_and_request_identity(provider):
    """OpenID discovery remains sufficient when OAuth metadata is absent."""
    provider["oauth"] = None
    provider["openid"]["scopes_supported"] += ["profile", "unrelated:admin"]
    provider["resource"]["scopes_supported"] = ["read", "read"]
    url = await oauth.begin_connect(
        NAME,
        SERVER,
        "https://druks.example.test",
        account_id=None,
        identity_mode=IdentityMode.SHARED,
    )
    params = dict(parse_qsl(urlparse(url).query))
    assert params["scope"] == "read openid email profile"


@pytest.mark.parametrize(("document", "scopes"), [("openid", "openid email"), ("resource", [1])])
async def test_invalid_scope_metadata_fails_before_registration(provider, document, scopes):
    """Malformed provider scope lists cannot become an authorization request."""
    provider[document]["scopes_supported"] = scopes
    with pytest.raises(OauthConnectError, match="invalid supported scopes"):
        await oauth.begin_connect(
            NAME,
            SERVER,
            "https://druks.example.test",
            account_id=None,
            identity_mode=IdentityMode.SHARED,
        )
    assert all(request.url.path != "/register" for request in provider["requests"])


@pytest.mark.parametrize("provider", ["atlassian"], indirect=True)
@pytest.mark.parametrize("field", ["issuer", "token_endpoint"])
async def test_root_openid_metadata_cannot_change_the_authority(provider, field):
    provider["openid"][field] = "https://auth.atlassian.com/unrelated"
    async with oauth._http() as client:
        metadata, identity, _ = await oauth._discover(client, NAME, provider["server"])
    assert metadata == provider["oauth"]
    assert identity == provider["oauth"]


@pytest.mark.parametrize("provider", ["atlassian"], indirect=True)
async def test_root_openid_metadata_cannot_replace_a_missing_scoped_authority(provider):
    provider["oauth"] = None
    async with oauth._http() as client:
        with pytest.raises(OauthConnectError, match="no authorization-server metadata"):
            await oauth._discover(client, NAME, provider["server"])


@pytest.mark.parametrize("provider", ["atlassian"], indirect=True)
async def test_scoped_oauth_issuer_cannot_replace_the_id_token_issuer(provider, druks_db):
    url = await oauth.begin_connect(
        NAME,
        provider["server"],
        "https://druks.example.test",
        account_id=None,
        identity_mode=IdentityMode.SHARED,
    )
    params = dict(parse_qsl(urlparse(url).query))
    provider["claims"].update(iss=provider["oauth"]["issuer"], nonce=params["nonce"])
    await oauth.complete_connect(state=params["state"], code="test-authorization-code")
    grant = await oauth.get_connection(NAME, None)
    assert grant.identity == {}
    assert grant.identity_status == "failed"
    assert not grant.revoked_at

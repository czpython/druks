import time

import httpx
import jwt as pyjwt
import pytest
from druks.mcp import oauth
from druks.mcp.exceptions import IdentityLookupError

ISSUER = "https://auth.example.test"
SERVER = "https://mcp.example.test/mcp"


@pytest.fixture
def pending():
    return {
        "name": "locally_renamed",
        "server_url": SERVER,
        "issuer": ISSUER,
        "client_id": "client-1",
        "nonce": "consent-nonce",
        "userinfo_endpoint": "",
    }


@pytest.fixture
def requests(monkeypatch):
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path == "/userinfo":
            return httpx.Response(200, json={"sub": "userinfo-subject", "email": "me@example.test"})
        return httpx.Response(404)

    monkeypatch.setattr(
        oauth, "_http", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    return requests


def token(**overrides):
    claims = {
        "iss": ISSUER,
        "aud": "client-1",
        "sub": "subject-1",
        "exp": int(time.time()) + 600,
        "iat": int(time.time()),
        "nonce": "consent-nonce",
        "name": "Operator",
        "email": "me@example.test",
        "email_verified": True,
        **overrides,
    }
    # Druks relies on TLS to the token endpoint and never checks this signature.
    return pyjwt.encode(claims, "local-signing-key-" * 2, algorithm="HS256")


@pytest.mark.parametrize(
    ("overrides", "claim"),
    [({"aud": "other-client"}, "aud"), ({"nonce": "other-consent"}, "nonce")],
)
async def test_a_mismatched_claim_fails_and_logs_the_claim(
    pending, requests, caplog, overrides, claim
):
    identity, status = await oauth.get_grant_identity(
        {"id_token": token(**overrides), "access_token": "access-secret"}, pending
    )
    assert (identity, status) == ({}, "failed")
    assert f"'{claim}'" in caplog.text


async def test_an_unreadable_id_token_falls_back_to_userinfo(pending, requests, caplog):
    pending["userinfo_endpoint"] = f"{ISSUER}/userinfo"
    identity, status = await oauth.get_grant_identity(
        {"id_token": "not-a-token", "access_token": "access-secret"}, pending
    )
    assert status == "resolved"
    assert identity["source"] == "userinfo"
    assert identity["subject"] == "userinfo-subject"
    assert requests[-1].headers["authorization"] == "Bearer access-secret"
    assert "access-secret" not in caplog.text
    assert "not-a-token" not in caplog.text


async def test_userinfo_failure_does_not_follow_redirect_or_expose_credentials(
    pending, monkeypatch, caplog
):
    pending["userinfo_endpoint"] = f"{ISSUER}/userinfo"
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            302, headers={"location": "https://other.example.test/collect"}, text="access-secret"
        )

    monkeypatch.setattr(
        oauth,
        "_http",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True),
    )
    assert await oauth.get_grant_identity({"access_token": "access-secret"}, pending) == (
        {},
        "failed",
    )
    assert len(requests) == 1
    assert "access-secret" not in caplog.text


@pytest.mark.parametrize("payload", [[], {}, {"sub": " "}])
def test_invalid_identity_shape_cannot_establish_a_subject(payload):
    with pytest.raises(IdentityLookupError):
        oauth.get_identity_facts(payload, authority=ISSUER, source="userinfo")


@pytest.mark.parametrize("email_verified", [False, "true"])
def test_email_verification_requires_a_provider_boolean(email_verified):
    identity = oauth.get_identity_facts(
        {"sub": "user-1", "email": "me@example.test", "email_verified": email_verified},
        authority=ISSUER,
        source="userinfo",
    )
    if isinstance(email_verified, bool):
        assert identity["email_verified"] is email_verified
    else:
        assert "email_verified" not in identity

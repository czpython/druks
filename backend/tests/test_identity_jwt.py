import time
from pathlib import Path

import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from druks.accounts import jwt as assertion
from druks.accounts.models import Account, PersonalAccessToken
from druks.testing import configure_app_for_test, make_settings
from fastapi.testclient import TestClient
from fastmcp.server.auth.providers.jwt import JWTVerifier

HEADER = "X-ExeDev-Email"
KID = "edge-key-1"
ISSUER = "https://edge.example.com"
AUDIENCE = "druks"

_PRIVATE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_FOREIGN_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_JWK = pyjwt.algorithms.RSAAlgorithm.to_jwk(_PRIVATE_KEY.public_key(), as_dict=True)
_JWKS = {"keys": [{**_JWK, "kid": KID}]}


@pytest.fixture(autouse=True)
def _serve_jwks(monkeypatch):
    assertion._verifier.cache_clear()

    async def fetch_jwks(self):
        return _JWKS

    monkeypatch.setattr(JWTVerifier, "_fetch_jwks", fetch_jwks)
    yield
    assertion._verifier.cache_clear()


def _token(key=_PRIVATE_KEY, **claim_overrides) -> str:
    claims = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "exp": int(time.time()) + 600,
        "email": "op@example.com",
        **claim_overrides,
    }
    claims = {name: value for name, value in claims.items() if value is not None}
    return pyjwt.encode(claims, key, algorithm="RS256", headers={"kid": KID})


def _jwt_client(tmp_path: Path) -> TestClient:
    app = configure_app_for_test(
        settings=make_settings(
            tmp_path,
            identity={
                "mode": "jwt",
                "header": HEADER,
                "jwks_url": "https://edge.example.com/jwks.json",
                "jwt_issuer": ISSUER,
                "jwt_audience": AUDIENCE,
            },
        ),
        authenticated=False,
    )
    return TestClient(app)


def test_a_valid_assertion_open_enrolls_its_subject(tmp_path, druks_db):
    with _jwt_client(tmp_path) as client:
        response = client.get("/api/auth/me", headers={HEADER: _token()})
        assert response.status_code == 200
        assert response.json()["account"]["username"] == "op@example.com"
        other = client.get("/api/auth/me", headers={HEADER: _token(email="two@example.com")})
        assert other.status_code == 200
    usernames = {account.username for account in Account.list_non_system()}
    assert usernames == {"op@example.com", "two@example.com"}


@pytest.mark.parametrize(
    "token",
    [
        _token(key=_FOREIGN_KEY),  # signature it can't verify
        _token(exp=int(time.time()) - 60),  # expired
        _token(exp=None),  # our contract requires exp even when the library allows its absence
        _token(iss="https://impostor.example.com"),
        _token(aud="not-druks"),
        _token(email=None),  # missing identity claim
        _token(email={"nested": "never"}),  # non-string identity claim
        "not.a.jwt",
    ],
)
def test_a_bad_assertion_rejects_without_enrolling(tmp_path, druks_db, token):
    with _jwt_client(tmp_path) as client:
        response = client.get("/api/auth/me", headers={HEADER: token})
        assert response.status_code == 401
        # Only the failure class reaches the caller — never token material.
        assert token.split(".")[1] not in response.json()["detail"]
    assert not Account.list_non_system()


def test_none_mode_multi_kid_document_serves_the_matching_key(tmp_path, druks_db):
    with _jwt_client(tmp_path) as client:
        assert client.get("/api/auth/me", headers={HEADER: _token()}).status_code == 200


def test_bearer_precedence_survives_jwt_mode(tmp_path, druks_db):
    agent = Account.get_or_create("agent@example.com")
    _, token = PersonalAccessToken.create(account_id=agent.id, name="agent")
    with _jwt_client(tmp_path) as client:
        # A valid bearer wins over any assertion, even a garbage one.
        response = client.get(
            "/api/auth/me",
            headers={"Authorization": f"Bearer {token}", HEADER: "not.a.jwt"},
        )
        assert response.status_code == 200
        assert response.json()["account"]["username"] == "agent@example.com"
        # An invalid bearer never falls through to a valid assertion.
        bad = client.get("/api/auth/me", headers={"Authorization": "Bearer nope", HEADER: _token()})
        assert bad.status_code == 401
        # PAT management admits the verified assertion alone — never a bearer.
        allowed = client.get("/api/auth/personal-tokens", headers={HEADER: _token()})
        assert allowed.status_code == 200
        managed = client.get(
            "/api/auth/personal-tokens",
            headers={"Authorization": f"Bearer {token}", HEADER: _token()},
        )
        assert managed.status_code == 401

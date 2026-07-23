import hashlib
import secrets
from datetime import timedelta
from pathlib import Path

import druks.redis
import pytest
from conftest import configure_app_for_test, make_settings
from druks.accounts.constants import PAT_TOKEN_TAG
from druks.accounts.exceptions import InvalidPatError
from druks.accounts.models import Account, PersonalAccessToken
from druks.database import db_session as session_registry
from druks.models import Base
from fastapi.testclient import TestClient

HEADER = "X-ExeDev-Email"
OPERATOR = {HEADER: "op@example.com"}


@pytest.fixture(autouse=True)
def _clear_redis():
    druks.redis.get_client()._data.clear()
    yield


def _client(tmp_path: Path, **settings_overrides) -> TestClient:
    settings_overrides.setdefault("auth_mode", "header")
    if settings_overrides["auth_mode"] == "header":
        settings_overrides.setdefault("auth_header", HEADER)
    app = configure_app_for_test(
        settings=make_settings(tmp_path, **settings_overrides), authenticated=False
    )
    return TestClient(app)


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _mint(username: str = "agent@example.com") -> tuple[PersonalAccessToken, str]:
    account = Account.get_or_create(username)
    return PersonalAccessToken.create(account_id=account.id, name="agent")


def test_the_minted_token_shape_and_hash_are_pinned(db_session):
    pat, token = _mint()
    prefix, _, secret = token.removeprefix(f"{PAT_TOKEN_TAG}_").partition("_")
    assert token.startswith(f"{PAT_TOKEN_TAG}_")
    assert len(prefix) == 12
    assert len(secret) == 43
    assert pat.token_prefix == prefix
    # The stored hash is SHA-256 of the full serialized token: exactly 32 bytes.
    assert len(pat.token_hash) == 32
    assert pat.token_hash == hashlib.sha256(token.encode()).digest()
    assert pat.expires_at == pat.created_at + timedelta(days=365)
    assert not pat.last_used_at
    assert pat.status == "active"


def test_a_prefix_collision_regenerates(db_session, monkeypatch):
    first, _ = _mint()
    replay = iter(first.token_prefix)
    random_choice = secrets.choice

    def collide_once(alphabet):
        # Feed the taken prefix back once, then return to real randomness.
        try:
            return next(replay)
        except StopIteration:
            return random_choice(alphabet)

    monkeypatch.setattr(secrets, "choice", collide_once)
    second, _ = PersonalAccessToken.create(account_id=first.account_id, name="two")
    assert second.token_prefix != first.token_prefix


def test_authenticate_rejects_everything_but_the_live_token(db_session):
    pat, token = _mint()
    assert PersonalAccessToken.authenticate(token).id == pat.id
    with pytest.raises(InvalidPatError):
        PersonalAccessToken.authenticate("not-even-shaped-right")
    with pytest.raises(InvalidPatError):
        PersonalAccessToken.authenticate(f"{PAT_TOKEN_TAG}_{pat.token_prefix}_wrongsecret")

    pat.expires_at = Base.utc_now() - timedelta(days=1)
    with pytest.raises(InvalidPatError, match=f"{pat.token_prefix} has expired"):
        PersonalAccessToken.authenticate(token)

    pat.expires_at = Base.utc_now() + timedelta(days=1)
    pat.revoke()
    with pytest.raises(InvalidPatError, match=f"{pat.token_prefix} was revoked"):
        PersonalAccessToken.authenticate(token)


def test_last_used_advances_at_most_hourly(db_session):
    pat, token = _mint()
    PersonalAccessToken.authenticate(token)
    first_use = pat.last_used_at
    assert first_use
    PersonalAccessToken.authenticate(token)
    assert pat.last_used_at == first_use
    pat.last_used_at = first_use - timedelta(hours=2)
    PersonalAccessToken.authenticate(token)
    assert pat.last_used_at > first_use - timedelta(hours=2)


def test_a_bearer_pat_authenticates_gated_routes(tmp_path, db_session):
    with _client(tmp_path) as client:
        _, token = _mint()
        response = client.get("/api/auth/me", headers=_bearer(token))
        assert response.status_code == 200
        assert response.json()["account"]["username"] == "agent@example.com"
        assert "set-cookie" not in response.headers
        assert client.get("/api/settings", headers=_bearer(token)).status_code == 200


def test_an_unknown_token_never_falls_through_to_the_assertion(tmp_path, db_session):
    with _client(tmp_path) as client:
        assert client.get("/api/auth/me", headers=OPERATOR).status_code == 200
        response = client.get(
            "/api/auth/me",
            headers={**OPERATOR, **_bearer(f"{PAT_TOKEN_TAG}_unknownpref1_nosecret")},
        )
        assert response.status_code == 401
        assert response.headers["WWW-Authenticate"] == 'Bearer realm="druks", error="invalid_token"'


@pytest.mark.parametrize("header", ["", "Token abc", "Bearer", "Bearer "])
def test_a_shapeless_authorization_header_is_challenged(tmp_path, db_session, header):
    with _client(tmp_path) as client:
        response = client.get("/api/auth/me", headers={"Authorization": header})
        assert response.status_code == 401
        assert response.headers["WWW-Authenticate"] == 'Bearer realm="druks"'


@pytest.mark.parametrize("header", ["Bearer a b", "bearer lowercased", "BEARER nope"])
def test_any_scheme_case_reaches_authentication_and_fails_closed(tmp_path, db_session, header):
    # RFC 7235 schemes are case-insensitive: these parse as credentials and die
    # in authentication — a 401 either way, never a slide to the assertion.
    with _client(tmp_path) as client:
        response = client.get(
            "/api/auth/me", headers={"Authorization": header, HEADER: "op@example.com"}
        )
        assert response.status_code == 401
        assert response.headers["WWW-Authenticate"].endswith('error="invalid_token"')
    assert not Account.get_for_username("op@example.com")


def test_an_empty_authorization_header_never_slides_to_the_assertion(tmp_path, db_session):
    with _client(tmp_path) as client:
        response = client.get("/api/auth/me", headers={**OPERATOR, "Authorization": ""})
        assert response.status_code == 401


def test_a_dead_token_401s_with_its_prefix_only(tmp_path, db_session):
    with _client(tmp_path) as client:
        pat, token = _mint()
        pat.revoke()
        response = client.get("/api/auth/me", headers=_bearer(token))
        assert response.status_code == 401
        assert response.headers["WWW-Authenticate"] == 'Bearer realm="druks", error="invalid_token"'
        assert pat.token_prefix in response.json()["detail"]
        _, _, secret = token.removeprefix(f"{PAT_TOKEN_TAG}_").partition("_")
        assert secret not in response.text


def test_a_pat_cannot_manage_pats(tmp_path, db_session):
    with _client(tmp_path) as client:
        pat, token = _mint()
        assert client.get("/api/auth/personal-tokens", headers=_bearer(token)).status_code == 401
        create = client.post(
            "/api/auth/personal-tokens", json={"name": "x"}, headers=_bearer(token)
        )
        assert create.status_code == 401
        revoke = client.delete(f"/api/auth/personal-tokens/{pat.id}", headers=_bearer(token))
        assert revoke.status_code == 401
        # Even riding beside a valid identity assertion, a bearer is refused —
        # management admits the session identity alone.
        both = client.get("/api/auth/personal-tokens", headers={**OPERATOR, **_bearer(token)})
        assert both.status_code == 401


def test_a_pat_cannot_disconnect_a_harness(tmp_path, db_session):
    # Disconnect destroys a capability a bearer could never create — the same
    # session-only rule as token management.
    with _client(tmp_path) as client:
        _, token = _mint("op@example.com")
        alone = client.delete("/api/harnesses/claude/connection", headers=_bearer(token))
        assert alone.status_code == 401
        beside = client.delete(
            "/api/harnesses/claude/connection", headers={**OPERATOR, **_bearer(token)}
        )
        assert beside.status_code == 401


def test_the_operator_manages_the_token_lifecycle(tmp_path, db_session):
    with _client(tmp_path) as client:
        created = client.post(
            "/api/auth/personal-tokens", json={"name": "ci bot"}, headers=OPERATOR
        )
        assert created.status_code == 200
        # Create answers only the plaintext, exactly once; the row surfaces
        # through the list.
        assert list(created.json()) == ["token"]
        token = created.json()["token"]
        assert token.startswith(f"{PAT_TOKEN_TAG}_")

        listed = client.get("/api/auth/personal-tokens", headers=OPERATOR).json()
        assert [item["name"] for item in listed] == ["ci bot"]
        assert token.split("_")[2] == listed[0]["prefix"]
        assert "token" not in listed[0]

        row_id = listed[0]["id"]
        revoked = client.delete(f"/api/auth/personal-tokens/{row_id}", headers=OPERATOR).json()
        assert revoked["status"] == "revoked"
        # A repeat revoke answers the same state, same instant.
        again = client.delete(f"/api/auth/personal-tokens/{row_id}", headers=OPERATOR).json()
        assert again["revokedAt"] == revoked["revokedAt"]


def test_the_none_mode_operator_manages_tokens_too(tmp_path, db_session):
    Account.get_or_create("op@example.com")
    with _client(tmp_path, auth_mode="none") as client:
        created = client.post("/api/auth/personal-tokens", json={"name": "local"})
        assert created.status_code == 200
        listed = client.get("/api/auth/personal-tokens").json()
    assert [item["name"] for item in listed] == ["local"]


def test_the_list_is_scoped_to_the_operator(tmp_path, db_session):
    with _client(tmp_path) as client:
        _mint("other@example.com")
        assert client.get("/api/auth/personal-tokens", headers=OPERATOR).json() == []


def test_revoking_anothers_token_is_a_404(tmp_path, db_session):
    with _client(tmp_path) as client:
        pat, _ = _mint("other@example.com")
        assert (
            client.delete(f"/api/auth/personal-tokens/{pat.id}", headers=OPERATOR).status_code
            == 404
        )
        session_registry().expire_all()
        assert not pat.revoked_at


def test_a_token_needs_a_name_that_fits(tmp_path, db_session):
    with _client(tmp_path) as client:
        no_name = client.post("/api/auth/personal-tokens", json={"name": "   "}, headers=OPERATOR)
        assert no_name.status_code == 422
        too_long = client.post(
            "/api/auth/personal-tokens", json={"name": "x" * 81}, headers=OPERATOR
        )
        assert too_long.status_code == 422

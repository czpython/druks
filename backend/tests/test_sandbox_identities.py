import hashlib
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from conftest import connect_provider, connect_service
from druks.core.services import Github
from druks.database import db_session
from druks.durable.engine import _step_engine
from druks.harnesses import providers as pbase
from druks.harnesses.providers import AnthropicProvider
from druks.sandbox.constants import SANDBOX_HOST_LEASE_SECONDS
from druks.sandbox.models import SandboxIdentity, SecretRef
from druks.testing import asgi_client, configure_app_for_test, make_settings, seed_run
from druks_field_notes.workflows import Summarize
from sqlalchemy.exc import IntegrityError


def _payload(*, access: str = "live", life: timedelta = timedelta(hours=6)) -> dict:
    expires_at = datetime.now(UTC) + life
    return {
        "claudeAiOauth": {
            "accessToken": access,
            "refreshToken": "R0",
            "scopes": ["user:profile"],
            "expiresAt": int(expires_at.timestamp() * 1000),
        }
    }


async def _subscription(email: str = "op@example.com", **kwargs):
    return await connect_provider(AnthropicProvider, _payload(**kwargs), provider_email=email)


def _anthropic(subscription) -> SecretRef:
    return SecretRef(name="anthropic", secret_id=subscription.id)


async def _bound_identity(subscription, *, state: str = "running") -> tuple[SandboxIdentity, str]:
    await seed_run(db_session(), kind=Summarize.kind, run_id="run-1", state=state)
    identity, entries = await SandboxIdentity.create(
        run_id="run-1", scoped_to="workflow", secret_refs=[_anthropic(subscription)]
    )
    await identity.bind("host-1")
    return identity, entries["anthropic"].headers["Authorization"].removeprefix("Bearer ")


def _resp(status: int, body: object) -> httpx.Response:
    return httpx.Response(status, text=json.dumps(body), request=httpx.Request("POST", "https://x"))


def _mock_post(monkeypatch, response):
    calls = []

    async def fake_post(self, url, *, json=None, **_kwargs):
        calls.append(url)
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr(pbase.httpx.AsyncClient, "post", fake_post)
    return calls


def _no_gate(subscription_id: str):
    raise AssertionError(f"a fresh token shut the gate of {subscription_id}")


async def _fetch(
    tmp_path, identity_id: str, bearer: str, name: str = "anthropic"
) -> httpx.Response:
    api = configure_app_for_test(settings=make_settings(tmp_path), authenticated=False)
    async with asgi_client(api) as client:
        return await client.get(
            f"/api/secrets/{identity_id}/{name}", headers={"Authorization": f"Bearer {bearer}"}
        )


async def test_an_identity_keeps_the_hash_and_puts_the_bearer_in_the_issuer_entry(druks_db):
    subscription = await _subscription()
    await seed_run(db_session(), kind=Summarize.kind, run_id="run-1")

    identity, entries = await SandboxIdentity.create(
        run_id="run-1", scoped_to="workflow", secret_refs=[_anthropic(subscription)]
    )

    [entry] = entries.values()
    bearer = entry.headers["Authorization"].removeprefix("Bearer ")
    assert entry.entry() == {
        "issuer": {
            "url": f"http://127.0.0.1:8001/api/secrets/{identity.id}/anthropic",
            "headers": {"Authorization": f"Bearer {bearer}"},
            "refresh": "1h",
        }
    }
    assert identity.token_hash == hashlib.sha256(bearer.encode()).digest()
    assert identity.expires_at - identity.created_at == timedelta(
        seconds=SANDBOX_HOST_LEASE_SECONDS
    )
    [secret] = identity.secret_refs
    assert secret.key == ("anthropic", subscription.id, "")
    rows = [identity, secret]
    columns = {
        column.name: getattr(row, column.name) for row in rows for column in row.__table__.columns
    }
    assert bearer not in repr(columns)
    assert not identity.host_id


async def test_one_box_holds_one_identity(druks_db):
    subscription = await _subscription()
    await seed_run(db_session(), kind=Summarize.kind, run_id="run-1")
    first, _ = await SandboxIdentity.create(
        run_id="run-1", scoped_to="workflow", secret_refs=[_anthropic(subscription)]
    )
    second, _ = await SandboxIdentity.create(
        run_id="run-1", scoped_to="workflow", secret_refs=[_anthropic(subscription)]
    )

    await first.bind("host-1")
    with pytest.raises(IntegrityError):
        await second.bind("host-1")


async def test_an_identity_needs_its_run(druks_db):
    subscription = await _subscription()

    with pytest.raises(IntegrityError):
        await SandboxIdentity.create(
            run_id="no-such-run", scoped_to="workflow", secret_refs=[_anthropic(subscription)]
        )


@pytest.mark.parametrize("source", ["none", "unknown"])
async def test_a_ref_names_a_vault_row_that_exists(druks_db, source):
    """The table says which vault row a secret comes from. Anything else is
    refused."""
    await seed_run(db_session(), kind=Summarize.kind, run_id="run-1")
    secret = {
        "none": SecretRef(name="github"),
        "unknown": SecretRef(name="github", secret_id="no-such-row"),
    }[source]

    with pytest.raises(IntegrityError):
        await SandboxIdentity.create(run_id="run-1", scoped_to="workflow", secret_refs=[secret])


async def test_the_issuer_answers_a_fresh_token_with_its_expiry(druks_db, tmp_path, monkeypatch):
    subscription = await _subscription(access="live")
    identity, bearer = await _bound_identity(subscription)
    posts = _mock_post(monkeypatch, _resp(200, {}))
    monkeypatch.setattr(pbase.gate, "shut", _no_gate)

    response = await _fetch(tmp_path, identity.id, bearer)

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    expiry = AnthropicProvider.load_token(subscription).expires_at
    assert response.json() == {"value": "live", "expires_at": expiry.isoformat()}
    assert posts == []


@pytest.mark.parametrize("wrong", ["bearer", "name", "identity", "revoked", "expired", "run"])
async def test_the_issuer_denies_a_wrong_bearer_name_or_dead_identity(druks_db, tmp_path, wrong):
    subscription = await _subscription(access="live")
    state = "finished" if wrong == "run" else "running"
    identity, bearer = await _bound_identity(subscription, state=state)
    identity_id, name = identity.id, "anthropic"
    if wrong == "bearer":
        bearer = "not-it"
    if wrong == "name":
        name = "github"
    if wrong == "identity":
        identity_id = "no-such-identity"
    if wrong == "revoked":
        await identity.revoke()
    if wrong == "expired":
        identity.expires_at = datetime.now(UTC) - timedelta(minutes=1)
        await db_session().commit()

    response = await _fetch(tmp_path, identity_id, bearer, name)

    assert response.status_code == 403
    assert "live" not in response.text


async def test_the_issuer_denies_an_identity_revoked_during_the_source_io(
    druks_db, tmp_path, monkeypatch
):
    subscription = await _subscription(access="live")
    identity, bearer = await _bound_identity(subscription)
    answer = AnthropicProvider.issue_token

    async def revoke_then_answer(self, subscription_id, **kwargs):
        await SandboxIdentity.revoke_for_host(_step_engine(), identity.host_id)
        return await answer(subscription_id, **kwargs)

    monkeypatch.setattr(AnthropicProvider, "issue_token", revoke_then_answer)

    response = await _fetch(tmp_path, identity.id, bearer)

    assert response.status_code == 403
    assert "live" not in response.text


async def test_the_issuer_answers_503_when_the_source_holds_nothing_valid(
    druks_db, tmp_path, monkeypatch
):
    subscription = await _subscription(access="old", life=-timedelta(minutes=1))
    identity, bearer = await _bound_identity(subscription)
    _mock_post(monkeypatch, httpx.ConnectError("boom"))

    response = await _fetch(tmp_path, identity.id, bearer)

    assert response.status_code == 503
    assert "old" not in response.text


async def test_lookup_finds_the_live_bound_identity_of_a_scope(druks_db) -> None:
    await seed_run(db_session(), kind=Summarize.kind, run_id="run-1")
    one = await _subscription("one@example.com")
    two = await _subscription("two@example.com")
    secrets = [_anthropic(one)]
    await SandboxIdentity.create(run_id="run-1", scoped_to="workflow", secret_refs=secrets)
    revoked, _ = await SandboxIdentity.create(
        run_id="run-1", scoped_to="workflow", secret_refs=secrets
    )
    await revoked.bind("host-revoked")
    await revoked.revoke()
    other_scope, _ = await SandboxIdentity.create(
        run_id="run-1", scoped_to="reviewer", secret_refs=secrets
    )
    await other_scope.bind("host-reviewer")
    other_secrets, _ = await SandboxIdentity.create(
        run_id="run-1", scoped_to="workflow", secret_refs=[_anthropic(two)]
    )
    await other_secrets.bind("host-other")
    live, _ = await SandboxIdentity.create(
        run_id="run-1", scoped_to="workflow", secret_refs=secrets
    )
    await live.bind("host-live")

    found = await SandboxIdentity.lookup("run-1", "workflow", secrets)

    assert found is not None
    assert found.id == live.id
    more = [*secrets, SecretRef(name="github", secret_id="other", resource="acme/widgets")]
    assert await SandboxIdentity.lookup("run-1", "workflow", more) is None
    assert await SandboxIdentity.lookup("run-2", "workflow", secrets) is None


async def _connect_github(slug: str = "github") -> None:
    await connect_service(
        slug,
        identity={"app_id": "1", "slug": "druks-operator"},
        secrets={"private_key": "operator-pem"},
    )


async def _github_identity() -> tuple[SandboxIdentity, str]:
    await seed_run(db_session(), kind=Summarize.kind, run_id="run-1")
    secret_id = (await Github.get()).id
    identity, entries = await SandboxIdentity.create(
        run_id="run-1",
        scoped_to="workflow",
        secret_refs=[SecretRef(name="github", secret_id=secret_id, resource="acme/widgets")],
    )
    await identity.bind("host-1")
    return identity, entries["github"].headers["Authorization"].removeprefix("Bearer ")


async def test_the_issuer_answers_github_through_the_stored_service_and_repo(
    druks_db, tmp_path, monkeypatch
):
    """Each fetch gets a new token for the stored repo from the stored
    identity. The request selects nothing, and the same box renews across
    short lifetimes."""
    expiry = datetime(2026, 9, 7, 18, 0, tzinfo=UTC)
    asked: list[str] = []
    values = iter(("ghs_one", "ghs_two"))

    async def issue_token(resource: str) -> tuple[str, datetime]:
        asked.append(resource)
        return next(values), expiry

    monkeypatch.setattr(Github, "issue_token", issue_token)
    await _connect_github()
    identity, bearer = await _github_identity()

    first = await _fetch(tmp_path, identity.id, bearer, "github?repo=other/repo&service=nobody")
    second = await _fetch(tmp_path, identity.id, bearer, "github")

    assert first.json() == {"value": "ghs_one", "expires_at": expiry.isoformat()}
    assert second.json()["value"] == "ghs_two"
    assert asked == ["acme/widgets", "acme/widgets"]


async def test_a_disconnected_service_ends_its_secrets(druks_db, tmp_path):
    await _connect_github()
    identity, bearer = await _github_identity()

    await db_session().delete(await Github.get())
    await db_session().commit()

    response = await _fetch(tmp_path, identity.id, bearer, "github")
    assert response.status_code == 403


async def test_the_issuer_answers_503_when_github_refuses(druks_db, tmp_path, monkeypatch):
    async def issue_token(resource: str) -> tuple[str, datetime]:
        raise RuntimeError("app not installed on acme/widgets")

    monkeypatch.setattr(Github, "issue_token", issue_token)
    await _connect_github()
    identity, bearer = await _github_identity()

    response = await _fetch(tmp_path, identity.id, bearer, "github")

    assert response.status_code == 503
    assert "installed" not in response.text

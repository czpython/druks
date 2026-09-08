import hashlib
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from conftest import connect_provider
from druks.database import db_session
from druks.durable.engine import _step_engine
from druks.harnesses import providers as pbase
from druks.harnesses.providers import AnthropicProvider
from druks.sandbox.constants import SANDBOX_HOST_LEASE_SECONDS
from druks.sandbox.models import SandboxGrant
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


async def _subscription(**kwargs):
    return await connect_provider(AnthropicProvider, _payload(**kwargs))


def _services(subscription) -> dict:
    return {"anthropic": subscription.id}


async def _bound_grant(subscription, *, state: str = "running") -> tuple[SandboxGrant, str]:
    await seed_run(db_session(), kind=Summarize.kind, run_id="run-1", state=state)
    grant, entries = await SandboxGrant.create(
        run_id="run-1", scoped_to="workflow", services=_services(subscription)
    )
    await grant.bind("host-1")
    return grant, entries["anthropic"].headers["Authorization"].removeprefix("Bearer ")


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
    tmp_path, grant_id: str, bearer: str, service: str = "anthropic"
) -> httpx.Response:
    api = configure_app_for_test(settings=make_settings(tmp_path), authenticated=False)
    async with asgi_client(api) as client:
        return await client.get(
            f"/api/secrets/{grant_id}/{service}", headers={"Authorization": f"Bearer {bearer}"}
        )


async def test_a_grant_keeps_the_hash_and_puts_the_bearer_in_the_issuer_entry(druks_db):
    subscription = await _subscription()
    await seed_run(db_session(), kind=Summarize.kind, run_id="run-1")

    grant, entries = await SandboxGrant.create(
        run_id="run-1", scoped_to="workflow", services=_services(subscription)
    )

    [entry] = entries.values()
    bearer = entry.headers["Authorization"].removeprefix("Bearer ")
    assert entry.entry() == {
        "issuer": {
            "url": f"http://127.0.0.1:8001/api/secrets/{grant.id}/anthropic",
            "headers": {"Authorization": f"Bearer {bearer}"},
            "refresh": "1h",
        }
    }
    assert grant.token_hash == hashlib.sha256(bearer.encode()).digest()
    assert grant.expires_at - grant.created_at == timedelta(seconds=SANDBOX_HOST_LEASE_SECONDS)
    columns = {
        column.name: getattr(grant, column.name) for column in SandboxGrant.__table__.columns
    }
    assert bearer not in repr(columns)
    assert not grant.host_id


async def test_one_box_holds_one_grant(druks_db):
    subscription = await _subscription()
    await seed_run(db_session(), kind=Summarize.kind, run_id="run-1")
    first, _ = await SandboxGrant.create(
        run_id="run-1", scoped_to="workflow", services=_services(subscription)
    )
    second, _ = await SandboxGrant.create(
        run_id="run-1", scoped_to="workflow", services=_services(subscription)
    )

    await first.bind("host-1")
    with pytest.raises(IntegrityError):
        await second.bind("host-1")


async def test_a_grant_needs_its_run(druks_db):
    subscription = await _subscription()

    with pytest.raises(IntegrityError):
        await SandboxGrant.create(
            run_id="no-such-run", scoped_to="workflow", services=_services(subscription)
        )


async def test_the_issuer_answers_a_fresh_token_with_its_expiry(druks_db, tmp_path, monkeypatch):
    subscription = await _subscription(access="live")
    grant, bearer = await _bound_grant(subscription)
    posts = _mock_post(monkeypatch, _resp(200, {}))
    monkeypatch.setattr(pbase.gate, "shut", _no_gate)

    response = await _fetch(tmp_path, grant.id, bearer)

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    expiry = AnthropicProvider.load_token(subscription).expires_at
    assert response.json() == {"value": "live", "expires_at": expiry.isoformat()}
    assert posts == []


@pytest.mark.parametrize("wrong", ["bearer", "service", "grant", "revoked", "expired", "run"])
async def test_the_issuer_denies_a_wrong_bearer_service_or_dead_grant(druks_db, tmp_path, wrong):
    subscription = await _subscription(access="live")
    state = "finished" if wrong == "run" else "running"
    grant, bearer = await _bound_grant(subscription, state=state)
    grant_id, service = grant.id, "anthropic"
    if wrong == "bearer":
        bearer = "not-it"
    if wrong == "service":
        service = "github"
    if wrong == "grant":
        grant_id = "no-such-grant"
    if wrong == "revoked":
        await grant.revoke()
    if wrong == "expired":
        grant.expires_at = datetime.now(UTC) - timedelta(minutes=1)
        await db_session().commit()

    response = await _fetch(tmp_path, grant_id, bearer, service)

    assert response.status_code == 403
    assert "live" not in response.text


async def test_the_issuer_denies_a_grant_revoked_during_the_source_io(
    druks_db, tmp_path, monkeypatch
):
    subscription = await _subscription(access="live")
    grant, bearer = await _bound_grant(subscription)
    answer = AnthropicProvider.issue_token

    async def revoke_then_answer(self, subscription_id, **kwargs):
        await SandboxGrant.revoke_for_host(_step_engine(), grant.host_id)
        return await answer(subscription_id, **kwargs)

    monkeypatch.setattr(AnthropicProvider, "issue_token", revoke_then_answer)

    response = await _fetch(tmp_path, grant.id, bearer)

    assert response.status_code == 403
    assert "live" not in response.text


async def test_the_issuer_answers_503_when_the_source_holds_nothing_valid(
    druks_db, tmp_path, monkeypatch
):
    subscription = await _subscription(access="old", life=-timedelta(minutes=1))
    grant, bearer = await _bound_grant(subscription)
    _mock_post(monkeypatch, httpx.ConnectError("boom"))

    response = await _fetch(tmp_path, grant.id, bearer)

    assert response.status_code == 503
    assert "old" not in response.text


async def test_lookup_finds_the_live_bound_grant_of_a_scope() -> None:
    await seed_run(db_session(), kind=Summarize.kind, run_id="run-1")
    services = {"anthropic": "sub-1"}
    await SandboxGrant.create(run_id="run-1", scoped_to="workflow", services=services)
    revoked, _ = await SandboxGrant.create(run_id="run-1", scoped_to="workflow", services=services)
    await revoked.bind("host-revoked")
    await revoked.revoke()
    other_scope, _ = await SandboxGrant.create(
        run_id="run-1", scoped_to="reviewer", services=services
    )
    await other_scope.bind("host-reviewer")
    other_services, _ = await SandboxGrant.create(
        run_id="run-1", scoped_to="workflow", services={"anthropic": "sub-2"}
    )
    await other_services.bind("host-other")
    live, _ = await SandboxGrant.create(run_id="run-1", scoped_to="workflow", services=services)
    await live.bind("host-live")

    found = await SandboxGrant.lookup("run-1", "workflow", services)

    assert found is not None
    assert found.id == live.id
    assert await SandboxGrant.lookup("run-1", "workflow", {"anthropic": "sub-3"}) is None
    assert await SandboxGrant.lookup("run-2", "workflow", services) is None

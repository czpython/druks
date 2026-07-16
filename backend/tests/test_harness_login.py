import base64
import json
from datetime import UTC, datetime, timedelta

import druks.redis
import httpx
import pytest
from druks.accounts.models import Account
from druks.harnesses import base as hbase
from druks.harnesses.claude import ClaudeHarness
from druks.harnesses.codex import CodexHarness
from druks.harnesses.exceptions import LoginError
from druks.harnesses.models import HarnessConnection


@pytest.fixture(autouse=True)
def _clear_pending():
    # The suite shares one in-memory fake Redis; clear the login pending keys so
    # one test's stash never leaks into another.
    druks.redis.get_client()._data.clear()
    yield


def _jwt(claims: dict) -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').rstrip(b"=").decode()
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    return f"{header}.{payload}.sig"


def _resp(status: int, body: object) -> httpx.Response:
    text = body if isinstance(body, str) else json.dumps(body)
    return httpx.Response(status, text=text, request=httpx.Request("POST", "https://x"))


def _mock_post(monkeypatch, response):
    calls = []

    async def fake_post(self, url, *, json=None, data=None, **_kwargs):
        calls.append({"url": url, "json": json, "data": data})
        return response

    monkeypatch.setattr(hbase.httpx.AsyncClient, "post", fake_post)
    return calls


async def _pending(flow_id: str) -> dict | None:
    raw = await druks.redis.get_client().get(f"druks:login:pending:{flow_id}")
    return json.loads(raw) if raw else None


_CLAUDE_GRANT = {
    "access_token": "AT",
    "refresh_token": "RT",
    "expires_in": 28800,
    "scope": "user:profile user:inference",
    "account": {"email_address": "me@example.com"},
}


async def test_claude_login_start_builds_url_and_stashes_pending(db_session):
    url, flow_id = await ClaudeHarness.login_start()
    assert url.startswith("https://claude.ai/oauth/authorize?")
    assert "code=true" in url
    assert "code_challenge_method=S256" in url
    assert "client_id=9d1c250a-e61b-44d9-88ed-5944d1962f5e" in url

    pending = await _pending(flow_id)
    assert pending["state"] == pending["verifier"]  # claude echoes the verifier as state


async def test_claude_login_complete_creates_account_and_login(monkeypatch, db_session):
    _, flow_id = await ClaudeHarness.login_start()
    calls = _mock_post(monkeypatch, _resp(200, _CLAUDE_GRANT))
    await ClaudeHarness.login_complete(flow_id=flow_id, pasted="thecode")

    login = HarnessConnection.get_default("claude")
    assert login is not None
    block = dict(login.payload)["claudeAiOauth"]
    assert block["accessToken"] == "AT"
    assert block["refreshToken"] == "RT"
    assert block["scopes"] == ["user:profile", "user:inference"]
    assert login.provider_email == "me@example.com"
    assert login.is_default is True
    assert Account.get_for_email("me@example.com").id == login.account_id
    # Claude exchanges JSON with the code + state echoed in the body.
    assert calls[0]["json"]["code"] == "thecode"
    assert "state" in calls[0]["json"]
    # Single-use: the pending state is gone.
    assert await _pending(flow_id) is None


async def test_concurrent_login_flows_do_not_clobber_each_other(monkeypatch, db_session):
    # Two operators connect the same harness at once: distinct flow ids, both
    # pendings live, and completing one leaves the other completable.
    _, first_flow = await ClaudeHarness.login_start()
    _, second_flow = await ClaudeHarness.login_start()
    assert first_flow != second_flow
    assert await _pending(first_flow) is not None
    assert await _pending(second_flow) is not None

    _mock_post(monkeypatch, _resp(200, _CLAUDE_GRANT))
    await ClaudeHarness.login_complete(flow_id=first_flow, pasted="code-1")
    assert await _pending(second_flow) is not None

    second_grant = dict(_CLAUDE_GRANT, account={"email_address": "other@example.com"})
    _mock_post(monkeypatch, _resp(200, second_grant))
    await ClaudeHarness.login_complete(flow_id=second_flow, pasted="code-2")

    connected = {login.provider_email for login in HarnessConnection.list_all()}
    assert connected == {"me@example.com", "other@example.com"}
    # The first login connected stays the harness default.
    assert HarnessConnection.get_default("claude").provider_email == "me@example.com"


async def test_login_complete_without_provider_email_raises(monkeypatch, db_session):
    _, flow_id = await ClaudeHarness.login_start()
    grant = dict(_CLAUDE_GRANT, account={})
    _mock_post(monkeypatch, _resp(200, grant))
    with pytest.raises(LoginError, match="no account email"):
        await ClaudeHarness.login_complete(flow_id=flow_id, pasted="thecode")
    assert HarnessConnection.list_all() == []


async def test_login_complete_matches_provider_email_case_insensitively(monkeypatch, db_session):
    # The provider's email is stored as given; the citext columns match it
    # regardless of case, so no account or connection duplicates on casing.
    _, flow_id = await ClaudeHarness.login_start()
    grant = dict(_CLAUDE_GRANT, account={"email_address": "Me@Example.COM"})
    _mock_post(monkeypatch, _resp(200, grant))
    await ClaudeHarness.login_complete(flow_id=flow_id, pasted="thecode")
    login = HarnessConnection.get_default("claude")
    assert login.provider_email == "Me@Example.COM"
    assert Account.get_for_email("me@example.com") is not None  # citext: case-insensitive


async def test_codex_login_complete_is_form_encoded_and_reads_jwt(monkeypatch, db_session):
    _, flow_id = await CodexHarness.login_start()
    pending = await _pending(flow_id)
    access = _jwt(
        {
            "https://api.openai.com/auth": {"chatgpt_account_id": "acc-9"},
            "https://api.openai.com/profile": {"email": "c@example.com"},
            "exp": int((datetime.now(UTC) + timedelta(days=10)).timestamp()),
        }
    )
    calls = _mock_post(
        monkeypatch, _resp(200, {"access_token": access, "refresh_token": "RT", "id_token": "ID"})
    )
    await CodexHarness.login_complete(
        flow_id=flow_id,
        pasted=f"http://localhost:1455/auth/callback?code=thecode&state={pending['state']}",
    )

    login = HarnessConnection.get_default("codex")
    payload = dict(login.payload)
    assert payload["tokens"]["account_id"] == "acc-9"
    assert payload["tokens"]["id_token"] == "ID"
    assert login.provider_email == "c@example.com"
    # Codex exchanges form-encoded, no state in the body.
    assert calls[0]["data"]["code"] == "thecode"
    assert "state" not in calls[0]["data"]


async def test_login_complete_unreadable_provider_json_raises_login_error(monkeypatch, db_session):
    _, flow_id = await ClaudeHarness.login_start()
    _mock_post(monkeypatch, _resp(200, "not json"))

    with pytest.raises(LoginError) as error:
        await ClaudeHarness.login_complete(flow_id=flow_id, pasted="code")

    assert "unreadable response" in str(error.value)


async def test_login_complete_without_pending_raises(db_session):
    with pytest.raises(LoginError):
        await ClaudeHarness.login_complete(flow_id="not-a-flow", pasted="code")


async def test_login_complete_state_mismatch_raises(db_session):
    _, flow_id = await ClaudeHarness.login_start()
    with pytest.raises(LoginError):
        await ClaudeHarness.login_complete(flow_id=flow_id, pasted="code#not-the-state")


async def test_login_complete_provider_error_clears_pending(monkeypatch, db_session):
    _, flow_id = await ClaudeHarness.login_start()
    _mock_post(monkeypatch, _resp(400, "invalid_grant: code expired"))
    with pytest.raises(LoginError) as error:
        await ClaudeHarness.login_complete(flow_id=flow_id, pasted="code")
    assert "invalid_grant" in str(error.value)
    # Failure is single-use too — a retry must re-start.
    assert await _pending(flow_id) is None
    assert HarnessConnection.get_default("claude") is None


def test_disconnect_deletes_the_default_row(db_session):
    from conftest import connect_harness

    connect_harness(ClaudeHarness, {"claudeAiOauth": {"accessToken": "x", "refreshToken": "r"}})
    assert HarnessConnection.get_default("claude") is not None
    ClaudeHarness.disconnect()
    assert HarnessConnection.get_default("claude") is None

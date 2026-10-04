import base64
import json
from datetime import UTC, datetime, timedelta

import druks.redis
import httpx
import pytest
from druks.harnesses import providers as pbase
from druks.harnesses.exceptions import ConnectError
from druks.harnesses.providers import AnthropicProvider, OpenAiProvider


def _jwt(claims: dict) -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').rstrip(b"=").decode()
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    return f"{header}.{payload}.sig"


def _resp(status: int, body: object) -> httpx.Response:
    text = body if isinstance(body, str) else json.dumps(body)
    return httpx.Response(status, text=text, request=httpx.Request("POST", "https://x"))


def _mock_post(monkeypatch, *responses):
    calls = []

    async def fake_post(self, url, *, json=None, data=None, **_kwargs):
        calls.append({"url": url, "json": json, "data": data})
        # The last response answers every later call.
        return responses[min(len(calls), len(responses)) - 1]

    monkeypatch.setattr(pbase.httpx.AsyncClient, "post", fake_post)
    return calls


async def _pending(flow_id: str) -> dict | None:
    raw = await druks.redis.get_client().get(f"druks:harness:connect:pending:{flow_id}")
    return json.loads(raw) if raw else None


_CLAUDE_GRANT = {
    "access_token": "AT",
    "refresh_token": "RT",
    "expires_in": 28800,
    "scope": "user:profile user:inference",
    "account": {"email_address": "me@example.com"},
}


async def test_claude_connect_start_builds_url_and_stashes_pending(druks_db):
    challenge = await AnthropicProvider.start_connection()
    url, flow_id = challenge["authorize_url"], challenge["connection_id"]
    assert url.startswith("https://claude.ai/oauth/authorize?")
    assert "code=true" in url
    assert "code_challenge_method=S256" in url
    assert "client_id=9d1c250a-e61b-44d9-88ed-5944d1962f5e" in url

    pending = await _pending(flow_id)
    assert pending["state"] == pending["verifier"]  # claude echoes the verifier as state
    # An unbound setup flow binds to nothing until account resolution.
    assert not pending["account_id"]


async def test_connect_start_binds_the_operator_account(druks_db):
    flow_id = (await AnthropicProvider.start_connection(account_id="acct-1"))["connection_id"]
    pending = await _pending(flow_id)
    assert pending["account_id"] == "acct-1"


async def test_claude_connect_complete_returns_the_exchange(monkeypatch, druks_db):
    flow_id = (await AnthropicProvider.start_connection(account_id="acct-1"))["connection_id"]
    calls = _mock_post(monkeypatch, _resp(200, _CLAUDE_GRANT))
    completed = await AnthropicProvider.complete_connection(flow_id=flow_id, pasted="thecode")

    block = completed.payload["claudeAiOauth"]
    assert block["accessToken"] == "AT"
    assert block["refreshToken"] == "RT"
    assert block["scopes"] == ["user:profile", "user:inference"]
    assert completed.provider_email == "me@example.com"
    assert completed.expires_at
    # The account the flow was started under rides along for resolution.
    assert completed.account_id == "acct-1"
    # Claude exchanges JSON with the code + state echoed in the body.
    assert calls[0]["json"]["code"] == "thecode"
    assert "state" in calls[0]["json"]
    # Single-use: the pending state is gone.
    assert not await _pending(flow_id)


async def test_concurrent_connect_flows_do_not_clobber_each_other(monkeypatch, druks_db):
    # Two people connect the same provider at once: distinct flow ids, both
    # pendings live, and completing one leaves the other completable.
    first_flow = (await AnthropicProvider.start_connection())["connection_id"]
    second_flow = (await AnthropicProvider.start_connection())["connection_id"]
    assert first_flow != second_flow

    _mock_post(monkeypatch, _resp(200, _CLAUDE_GRANT))
    first = await AnthropicProvider.complete_connection(flow_id=first_flow, pasted="code-1")
    assert await _pending(second_flow)

    second_grant = dict(_CLAUDE_GRANT, account={"email_address": "other@example.com"})
    _mock_post(monkeypatch, _resp(200, second_grant))
    second = await AnthropicProvider.complete_connection(flow_id=second_flow, pasted="code-2")

    assert first.provider_email == "me@example.com"
    assert second.provider_email == "other@example.com"


async def test_connect_complete_without_provider_email_raises(monkeypatch, druks_db):
    flow_id = (await AnthropicProvider.start_connection())["connection_id"]
    grant = dict(_CLAUDE_GRANT, account={})
    _mock_post(monkeypatch, _resp(200, grant))
    with pytest.raises(ConnectError, match="no account email"):
        await AnthropicProvider.complete_connection(flow_id=flow_id, pasted="thecode")


_DEVICE_CODE = {"device_auth_id": "device-1", "user_code": "ABCD-EFGH", "interval": "5"}


async def test_codex_connect_start_requests_a_device_code(monkeypatch, druks_db):
    calls = _mock_post(monkeypatch, _resp(200, _DEVICE_CODE))
    challenge = await OpenAiProvider.start_connection(account_id="acct-1")

    assert calls[0]["json"] == {"client_id": OpenAiProvider._CLIENT_ID}
    assert challenge == {
        "method": "device",
        "connection_id": challenge["connection_id"],
        "authorize_url": "https://auth.openai.com/codex/device",
        "user_code": "ABCD-EFGH",
        "poll_interval": 5,
    }
    assert await _pending(challenge["connection_id"]) == {
        "device_auth_id": "device-1",
        "user_code": "ABCD-EFGH",
        "account_id": "acct-1",
    }


@pytest.mark.parametrize("status", [403, 404])
async def test_codex_connect_check_waits_for_approval(monkeypatch, druks_db, status):
    _mock_post(monkeypatch, _resp(200, _DEVICE_CODE), _resp(status, ""))
    flow_id = (await OpenAiProvider.start_connection())["connection_id"]

    assert not await OpenAiProvider.check_connection(flow_id=flow_id)
    assert await _pending(flow_id)


async def test_codex_connect_check_exchanges_the_approved_code(monkeypatch, druks_db):
    access = _jwt(
        {
            "https://api.openai.com/auth": {"chatgpt_account_id": "acc-9"},
            "https://api.openai.com/profile": {"email": "c@example.com"},
            "exp": int((datetime.now(UTC) + timedelta(days=10)).timestamp()),
        }
    )
    calls = _mock_post(
        monkeypatch,
        _resp(200, _DEVICE_CODE),
        _resp(200, {"authorization_code": "thecode", "code_verifier": "theverifier"}),
        _resp(200, {"access_token": access, "refresh_token": "RT", "id_token": "ID"}),
    )
    flow_id = (await OpenAiProvider.start_connection(account_id="acct-1"))["connection_id"]
    completed = await OpenAiProvider.check_connection(flow_id=flow_id)

    assert calls[1]["json"] == {"device_auth_id": "device-1", "user_code": "ABCD-EFGH"}
    assert completed.payload["tokens"]["account_id"] == "acc-9"
    assert completed.payload["tokens"]["id_token"] == "ID"
    assert completed.provider_email == "c@example.com"
    assert completed.account_id == "acct-1"
    # Codex exchanges form-encoded, with the verifier OpenAI returned.
    assert calls[2]["data"]["code"] == "thecode"
    assert calls[2]["data"]["code_verifier"] == "theverifier"
    assert calls[2]["data"]["redirect_uri"] == "https://auth.openai.com/deviceauth/callback"
    # The attempt is single-use.
    assert not await _pending(flow_id)


async def test_codex_connect_check_rejected_device_code_raises(monkeypatch, druks_db):
    _mock_post(monkeypatch, _resp(200, _DEVICE_CODE), _resp(400, "expired"))
    flow_id = (await OpenAiProvider.start_connection())["connection_id"]
    with pytest.raises(ConnectError, match="HTTP 400"):
        await OpenAiProvider.check_connection(flow_id=flow_id)


async def test_codex_connect_check_without_pending_raises(druks_db):
    with pytest.raises(ConnectError, match="expired"):
        await OpenAiProvider.check_connection(flow_id="not-a-flow")


async def test_connect_complete_unreadable_provider_json_raises_connect_error(
    monkeypatch, druks_db
):
    flow_id = (await AnthropicProvider.start_connection())["connection_id"]
    _mock_post(monkeypatch, _resp(200, "not json"))

    with pytest.raises(ConnectError) as error:
        await AnthropicProvider.complete_connection(flow_id=flow_id, pasted="code")

    assert "unreadable response" in str(error.value)


async def test_connect_complete_without_pending_raises(druks_db):
    with pytest.raises(ConnectError):
        await AnthropicProvider.complete_connection(flow_id="not-a-flow", pasted="code")


async def test_connect_complete_state_mismatch_raises(druks_db):
    flow_id = (await AnthropicProvider.start_connection())["connection_id"]
    with pytest.raises(ConnectError):
        await AnthropicProvider.complete_connection(flow_id=flow_id, pasted="code#not-the-state")


async def test_connect_complete_provider_error_clears_pending(monkeypatch, druks_db):
    flow_id = (await AnthropicProvider.start_connection())["connection_id"]
    _mock_post(monkeypatch, _resp(400, "invalid_grant: code expired"))
    with pytest.raises(ConnectError) as error:
        await AnthropicProvider.complete_connection(flow_id=flow_id, pasted="code")
    assert "invalid_grant" in str(error.value)
    # Failure is single-use too — a retry must re-start.
    assert not await _pending(flow_id)

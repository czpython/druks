import hashlib
import hmac

import pytest
from druks.apps.registry import webhooks
from druks.settings import Settings
from druks.webhooks import InvalidWebhookError, Webhook, verify_hmac_sha256
from druks.webhooks.router import match_webhook
from druks.webhooks.router import router as webhooks_router
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse, Response
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def _isolated_registry():
    """Snapshot the global webhook registry around every test.

    Defining Webhook subclasses registers them globally via
    ``__init_subclass__``. Restore the pre-test state so tests don't
    bleed registrations into each other.
    """
    snapshot = dict(webhooks._items)
    try:
        yield
    finally:
        webhooks._items = snapshot


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        data_dir=tmp_path,
        secrets={
            "secrets_key": "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA=",
        },
    )


# Registration & path derivation


def test_concrete_subclass_registers_automatically():
    class Hook(Webhook):
        provider = "acme"
        category = "events"

        def request_is_authentic(self):
            return True

        def get_action(self):
            return "noop"

    assert Hook in webhooks.all()
    assert Hook.path == "acme/events/"


def test_abstract_subclass_does_not_register():
    class Mid(Webhook):
        abstract = True

    assert Mid not in webhooks.all()


def test_grandchild_of_abstract_does_register():
    class Mid(Webhook):
        abstract = True

    class Hook(Mid):
        provider = "acme"
        category = "alerts"

        def request_is_authentic(self):
            return True

        def get_action(self):
            return "noop"

    assert Hook in webhooks.all()
    assert Mid not in webhooks.all()


def test_concrete_without_provider_and_category_raises():
    with pytest.raises(TypeError, match="provider"):

        class Hook(Webhook):
            def request_is_authentic(self):
                return True

            def get_action(self):
                return "noop"


def test_explicit_path_overrides_provider_category_default():
    class Hook(Webhook):
        path = "custom/path/"
        provider = "ignored"
        category = "ignored"

        def request_is_authentic(self):
            return True

        def get_action(self):
            return "noop"

    assert Hook.path == "custom/path/"


# Registry lookup


def test_registry_lookup_returns_class_and_empty_kwargs():
    class Hook(Webhook):
        provider = "alpha"
        category = "events"

        def request_is_authentic(self):
            return True

        def get_action(self):
            return "noop"

    cls, kwargs = match_webhook("alpha/events/")
    assert cls is Hook
    assert kwargs == {}


def test_registry_lookup_extracts_url_params():
    class Hook(Webhook):
        path = "tenant/{tenant_id}/events/"
        provider = "tenant"
        category = "events"

        def request_is_authentic(self):
            return True

        def get_action(self):
            return "noop"

    cls, kwargs = match_webhook("tenant/abc-123/events/")
    assert cls is Hook
    assert kwargs == {"tenant_id": "abc-123"}


def test_registry_lookup_misses_raise_invalid_webhook_error():
    with pytest.raises(InvalidWebhookError):
        match_webhook("unknown/path/")


def test_lookup_picks_up_a_late_registration():
    with pytest.raises(InvalidWebhookError):
        match_webhook("late/events/")

    class Hook(Webhook):
        provider = "late"
        category = "events"

        def request_is_authentic(self):
            return True

        def get_action(self):
            return "noop"

    cls, _ = match_webhook("late/events/")
    assert cls is Hook


# respond() dispatch


def _client_for(*hook_classes: type[Webhook], settings: Settings) -> TestClient:
    """Build a tiny FastAPI app mounting just the webhooks router.

    The hook classes are passed in for clarity (they self-register on
    definition; this just documents which hooks the test exercises).
    Use as a context manager: delivery marking talks to Redis, and only the
    ``with`` form gives every request one shared event loop for that client.
    """
    app = FastAPI()
    app.state.settings = settings
    app.include_router(webhooks_router)
    return TestClient(app, raise_server_exceptions=False)


def test_dispatch_calls_matching_on_action_method(settings):
    class Hook(Webhook):
        provider = "dispatch"
        category = "events"

        def request_is_authentic(self):
            return True

        def get_action(self):
            return "ping"

        async def on_ping(self):
            return JSONResponse({"pong": self.data.get("seq")})

    with _client_for(Hook, settings=settings) as client:
        response = client.post("/_external/dispatch/events/", json={"seq": 7})
    assert response.status_code == 200
    assert response.json() == {"pong": 7}


def test_dispatch_unhandled_action_falls_through_to_on_unhandled(settings):
    seen: list[str] = []

    class Hook(Webhook):
        provider = "fallback"
        category = "events"

        def request_is_authentic(self):
            return True

        def get_action(self):
            return "no_method_for_this"

        async def on_unhandled(self, action: str) -> Response:
            seen.append(action)
            return JSONResponse({"unhandled": action}, status_code=202)

    with _client_for(Hook, settings=settings) as client:
        response = client.post("/_external/fallback/events/", json={})
    assert response.status_code == 202
    assert response.json() == {"unhandled": "no_method_for_this"}
    assert seen == ["no_method_for_this"]


def test_dispatch_request_is_authentic_false_raises_401(settings):
    class Hook(Webhook):
        provider = "auth"
        category = "events"

        def request_is_authentic(self):
            return False

        def get_action(self):
            return "ping"

    with _client_for(Hook, settings=settings) as client:
        response = client.post("/_external/auth/events/", json={})
    assert response.status_code == 401


def test_dispatch_request_is_authentic_may_raise_http_exception(settings):
    class Hook(Webhook):
        provider = "rich"
        category = "events"

        def request_is_authentic(self):
            raise HTTPException(status_code=403, detail="Forbidden by policy.")

        def get_action(self):
            return "ping"

    with _client_for(Hook, settings=settings) as client:
        response = client.post("/_external/rich/events/", json={})
    assert response.status_code == 403


def test_unknown_path_returns_404(settings):
    with _client_for(settings=settings) as client:
        response = client.post("/_external/nothing/here/", json={})
    assert response.status_code == 404


def test_trailing_slash_is_optional_on_request_url(settings):
    class Hook(Webhook):
        provider = "loose"
        category = "events"

        def request_is_authentic(self):
            return True

        def get_action(self):
            return "ping"

        async def on_ping(self):
            return JSONResponse({"ok": True})

    with _client_for(Hook, settings=settings) as client:
        response = client.post("/_external/loose/events", json={})
    assert response.status_code == 200


# Deduplication


def test_dedup_first_call_dispatches_second_call_short_circuits(settings):
    seen_pings: list[int] = []

    class Hook(Webhook):
        provider = "dedup"
        category = "events"

        def request_is_authentic(self):
            return True

        def get_action(self):
            return "ping"

        def delivery_key(self):
            return self.data["key"]

        async def on_ping(self):
            seen_pings.append(self.data["seq"])
            return JSONResponse({"ok": True})

    with _client_for(Hook, settings=settings) as client:
        r1 = client.post("/_external/dedup/events/", json={"key": "K", "seq": 1})
        assert r1.status_code == 200
        assert r1.json() == {"ok": True}

        r2 = client.post("/_external/dedup/events/", json={"key": "K", "seq": 2})
        assert r2.status_code == 200
        assert r2.json() == {"accepted": False, "duplicate": True}

    assert seen_pings == [1]


def test_failed_handler_releases_claim_so_retry_reprocesses(settings):
    # A handler that raises must not consume the delivery: the claim is released so the
    # provider's retry runs the handler again instead of short-circuiting as a duplicate.
    attempts: list[int] = []

    class Hook(Webhook):
        provider = "flaky"
        category = "events"

        def request_is_authentic(self):
            return True

        def delivery_key(self):
            return self.data["key"]

        def get_action(self):
            return "ping"

        async def on_ping(self):
            attempts.append(self.data["seq"])
            if len(attempts) == 1:
                raise RuntimeError("transient handler failure")
            return JSONResponse({"ok": True})

    with _client_for(Hook, settings=settings) as client:
        r1 = client.post("/_external/flaky/events/", json={"key": "K", "seq": 1})
        assert r1.status_code == 500  # handler raised; claim released

        r2 = client.post("/_external/flaky/events/", json={"key": "K", "seq": 2})
        assert r2.status_code == 200
        assert r2.json() == {"ok": True}
    assert attempts == [1, 2]  # reprocessed, not swallowed as a duplicate


# Signature helper


def test_verify_hmac_sha256_accepts_correct_signature():
    body = b'{"hello":"world"}'
    secret = "supersecret"
    sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()

    # Should not raise.
    verify_hmac_sha256(body, sig, secret)


def test_verify_hmac_sha256_rejects_bad_signature():
    with pytest.raises(HTTPException) as exc_info:
        verify_hmac_sha256(b"{}", "sha256=bogus", "secret")
    assert exc_info.value.status_code == 401


def test_verify_hmac_sha256_missing_signature_is_401():
    with pytest.raises(HTTPException) as exc_info:
        verify_hmac_sha256(b"{}", None, "secret")
    assert exc_info.value.status_code == 401


def test_verify_hmac_sha256_missing_secret_is_500():
    with pytest.raises(HTTPException) as exc_info:
        verify_hmac_sha256(b"{}", "sha256=anything", "")
    assert exc_info.value.status_code == 500

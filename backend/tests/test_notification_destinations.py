from types import SimpleNamespace

import httpx
import pytest
from druks.notifications import delivery
from druks.notifications.buttons import decode_button, encode_button
from druks.notifications.exceptions import (
    DeliveryError,
    DisabledDestinationError,
    MalformedButtonError,
    UnknownDestinationKindError,
)
from druks.notifications.models import Destination, DestinationKind
from druks.testing import configure_app_for_test, make_settings
from fastapi.testclient import TestClient

_WEBHOOK_URL = "https://hooks.slack.com/services/T000/B000/secretpart"


def _slack_destination(*, is_enabled: bool = True) -> Destination:
    # Delivery never touches the database; an unsaved row is enough.
    return Destination(
        name="ops",
        kind=DestinationKind.SLACK_WEBHOOK.value,
        url=_WEBHOOK_URL,
        is_enabled=is_enabled,
    )


class _FakeApprise:
    def __init__(self):
        self.added: list[str] = []
        self.sent: list[dict] = []
        self.add_result = True
        self.notify_result = True
        self.notify_error: Exception | None = None

    def add(self, url: str) -> bool:
        self.added.append(url)
        return self.add_result

    async def async_notify(self, **kwargs) -> bool:
        if self.notify_error:
            raise self.notify_error
        if not self.add_result:
            # Real apprise: a failed add leaves zero servers and notify
            # returns False without sending.
            return False
        self.sent.append(kwargs)
        return self.notify_result


class _FakeSlack:
    def __init__(self):
        self.posts: list[tuple[str, dict]] = []
        self.status_code = 200

    def client(self, **kwargs):
        slack = self

        class _Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc_info):
                return False

            async def post(self, url: str, json: dict | None = None):
                slack.posts.append((url, json))
                return httpx.Response(slack.status_code, request=httpx.Request("POST", url))

        return _Client()


@pytest.fixture
def fake_apprise(monkeypatch):
    fake = _FakeApprise()
    monkeypatch.setattr(delivery, "apprise", SimpleNamespace(Apprise=lambda: fake))
    return fake


@pytest.fixture
def fake_slack(monkeypatch):
    fake = _FakeSlack()
    monkeypatch.setattr(
        delivery, "httpx", SimpleNamespace(AsyncClient=fake.client, HTTPError=httpx.HTTPError)
    )
    return fake


# --- registry: CRUD round-trip + kind gate --------------------------------


def test_create_get_list_delete_round_trip(druks_db):
    beta = Destination.create(name="beta", kind="slack_webhook", url=_WEBHOOK_URL)
    alpha = Destination.create(name="alpha", kind="slack_webhook", url=_WEBHOOK_URL)

    assert Destination.get(beta.id).id == beta.id
    assert Destination.get_for_name("alpha").id == alpha.id
    assert Destination.get("no-such-id") is None
    assert Destination.get_for_name("no-such-name") is None
    assert [destination.name for destination in Destination.list_all()] == ["alpha", "beta"]
    assert beta.is_enabled is True

    beta.delete()
    assert Destination.get_for_name("beta") is None
    assert [destination.name for destination in Destination.list_all()] == ["alpha"]


def test_create_rejects_unknown_kind_without_echoing_the_url(druks_db):
    with pytest.raises(UnknownDestinationKindError) as excinfo:
        Destination.create(name="pager", kind="pagerduty", url=_WEBHOOK_URL)

    assert "pagerduty" in str(excinfo.value)
    assert _WEBHOOK_URL not in str(excinfo.value)
    assert Destination.get_for_name("pager") is None


# --- routes: CRUD + redaction ---------------------------------------------


def _create_body(**overrides) -> dict:
    body = {"name": "ops", "kind": "slack_webhook", "url": _WEBHOOK_URL}
    body.update(overrides)
    return body


def test_routes_create_masks_url(tmp_path, druks_db):
    with TestClient(configure_app_for_test(settings=make_settings(tmp_path))) as client:
        created = client.post("/api/notifications/destinations", json=_create_body())

        assert created.status_code == 200
        body = created.json()
        assert body["name"] == "ops"
        assert body["kind"] == "slack_webhook"
        assert body["isEnabled"] is True
        assert body["hasUrl"] is True
        assert body["url"] == "**********"
        assert _WEBHOOK_URL not in created.text
        assert "secretpart" not in created.text


def test_routes_reject_duplicate_name(tmp_path, druks_db):
    with TestClient(configure_app_for_test(settings=make_settings(tmp_path))) as client:
        first = client.post("/api/notifications/destinations", json=_create_body())
        assert first.status_code == 200

        duplicate = client.post("/api/notifications/destinations", json=_create_body())
        assert duplicate.status_code == 409
        assert "secretpart" not in duplicate.text


def test_routes_reject_unknown_kind(tmp_path, druks_db):
    with TestClient(configure_app_for_test(settings=make_settings(tmp_path))) as client:
        created = client.post(
            "/api/notifications/destinations", json=_create_body(kind="pagerduty")
        )
        assert created.status_code == 422
        assert "secretpart" not in created.text
        assert not client.get("/api/notifications/destinations").json()


def test_routes_reject_undeliverable_url(tmp_path, druks_db):
    # Save-time deliverability: the same offline apprise parse the send path
    # uses, so a typo fails while the operator is present — not at first park.
    with TestClient(configure_app_for_test(settings=make_settings(tmp_path))) as client:
        for bad_url in ("https://example.com/hook", "not-a-url"):
            created = client.post("/api/notifications/destinations", json=_create_body(url=bad_url))
            assert created.status_code == 422
            assert bad_url not in created.text
        assert not client.get("/api/notifications/destinations").json()


def test_routes_reject_blank_name(tmp_path, druks_db):
    with TestClient(configure_app_for_test(settings=make_settings(tmp_path))) as client:
        for blank in ("", "   "):
            created = client.post("/api/notifications/destinations", json=_create_body(name=blank))
            assert created.status_code == 422
            assert "secretpart" not in created.text
        assert not client.get("/api/notifications/destinations").json()


def test_routes_toggle_enabled(tmp_path, druks_db):
    with TestClient(configure_app_for_test(settings=make_settings(tmp_path))) as client:
        destination_id = client.post("/api/notifications/destinations", json=_create_body()).json()[
            "id"
        ]

        toggled = client.patch(
            f"/api/notifications/destinations/{destination_id}", json={"is_enabled": False}
        )
        assert toggled.status_code == 200
        assert toggled.json()["isEnabled"] is False

        listed = client.get("/api/notifications/destinations").json()
        assert listed[0]["isEnabled"] is False

        back_on = client.patch(
            f"/api/notifications/destinations/{destination_id}", json={"is_enabled": True}
        )
        assert back_on.json()["isEnabled"] is True

        missing = client.patch(
            "/api/notifications/destinations/no-such-id", json={"is_enabled": False}
        )
        assert missing.status_code == 404


def test_routes_delete(tmp_path, druks_db):
    with TestClient(configure_app_for_test(settings=make_settings(tmp_path))) as client:
        destination_id = client.post("/api/notifications/destinations", json=_create_body()).json()[
            "id"
        ]

        assert client.delete(f"/api/notifications/destinations/{destination_id}").status_code == 204
        assert not client.get("/api/notifications/destinations").json()
        assert client.delete(f"/api/notifications/destinations/{destination_id}").status_code == 404


# --- informational delivery (Apprise) --------------------------------------


async def test_informational_delivery_sends_body_once(fake_apprise, fake_slack):
    await delivery.deliver(_slack_destination(), "build 42 finished")

    assert fake_apprise.added == [_WEBHOOK_URL]
    assert fake_apprise.sent == [{"body": "build 42 finished"}]
    assert fake_slack.posts == []


async def test_informational_delivery_unparseable_stored_url_still_fails_loudly(
    fake_apprise, caplog
):
    # Save-time validation makes this near-impossible (an apprise upgrade
    # dropping a plugin, a hand-edited row) — but the falsy notify result
    # still surfaces it as a sanitized failure, never a silent no-op.
    fake_apprise.add_result = False

    with pytest.raises(DeliveryError) as excinfo:
        await delivery.deliver(_slack_destination(), "hello")

    assert "ops" in str(excinfo.value)
    assert fake_apprise.sent == []
    assert _WEBHOOK_URL not in str(excinfo.value)
    assert _WEBHOOK_URL not in caplog.text


async def test_informational_delivery_notify_raising_is_sanitized(fake_apprise, caplog):
    fake_apprise.notify_error = RuntimeError(f"could not reach {_WEBHOOK_URL}")

    with pytest.raises(DeliveryError) as excinfo:
        await delivery.deliver(_slack_destination(), "hello")

    assert _WEBHOOK_URL not in str(excinfo.value)
    assert "RuntimeError" in str(excinfo.value)
    # The chain is severed so a rendered traceback can't resurface the URL.
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__suppress_context__ is True
    assert _WEBHOOK_URL not in caplog.text


# --- actionable delivery (Slack Block Kit) ---------------------------------


async def test_actionable_delivery_posts_block_kit_buttons(fake_apprise, fake_slack):
    actions = [{"id": "approve", "label": "Approve"}, {"id": "reject", "label": "Reject"}]

    await delivery.deliver(
        _slack_destination(), "review the plan", actions=actions, token="tok_123"
    )

    assert fake_apprise.added == []
    ((url, message),) = fake_slack.posts
    assert url == _WEBHOOK_URL
    assert message["text"] == "review the plan"
    section, action_block = message["blocks"]
    assert section["text"]["text"] == "review the plan"
    assert [button["text"]["text"] for button in action_block["elements"]] == [
        "Approve",
        "Reject",
    ]
    for button, action in zip(action_block["elements"], actions, strict=True):
        assert button["action_id"] == encode_button("tok_123", action["id"])
        assert decode_button(button["action_id"]) == ("tok_123", action["id"])


async def test_actionable_delivery_failure_is_sanitized(fake_apprise, fake_slack, caplog):
    fake_slack.status_code = 404

    with pytest.raises(DeliveryError) as excinfo:
        await delivery.deliver(
            _slack_destination(), "review", actions=[{"id": "ok", "label": "OK"}], token="tok"
        )

    assert _WEBHOOK_URL not in str(excinfo.value)
    assert excinfo.value.__cause__ is None
    assert _WEBHOOK_URL not in caplog.text


async def test_disabled_destination_never_sends(fake_apprise, fake_slack):
    disabled = _slack_destination(is_enabled=False)

    with pytest.raises(DisabledDestinationError):
        await delivery.deliver(disabled, "hello")
    with pytest.raises(DisabledDestinationError):
        await delivery.deliver(
            disabled, "hello", actions=[{"id": "ok", "label": "OK"}], token="tok"
        )

    assert fake_apprise.added == []
    assert fake_apprise.sent == []
    assert fake_slack.posts == []


# --- buttons: opaque round-trip --------------------------------------------


def test_button_encoding_round_trips():
    for token, choice_id in [
        ("tok_123", "approve"),
        ("with.dot", "choice.with.dots"),
        ("unicode-✓", "weird/chars?&="),
        ("", "empty-token-still-round-trips"),
    ]:
        action_id = encode_button(token, choice_id)
        assert decode_button(action_id) == (token, choice_id)
        assert "/" not in action_id
        assert "?" not in action_id


def test_button_decode_rejects_malformed_input():
    for malformed in ("no-separator", "bad!chars.x", "a.b!!", "tok.choice.extra"):
        with pytest.raises(MalformedButtonError) as excinfo:
            decode_button(malformed)
        assert malformed not in str(excinfo.value)


def test_button_decode_rejects_non_string_input():
    # A tampered webhook payload can put any JSON value where the action_id
    # belongs; the typed error must cover those too.
    for malformed in (None, 123, ["a.b"], {"action_id": "a.b"}):
        with pytest.raises(MalformedButtonError):
            decode_button(malformed)

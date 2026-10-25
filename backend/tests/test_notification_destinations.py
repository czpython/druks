from types import SimpleNamespace

import pytest
from conftest import bind_ambient_session, install_service
from druks.core.apis.slack import SlackClient
from druks.notifications.buttons import decode_button, encode_button
from druks.notifications.delivery import deliver
from druks.notifications.exceptions import (
    DeliveryError,
    DisabledDestinationError,
    MalformedButtonError,
)
from druks.notifications.models import Destination, DestinationKind
from druks.testing import asgi_client, configure_app_for_test, make_settings
from slack_sdk.errors import SlackApiError

_BOT_TOKEN = "xoxb-destinations"


def _slack_destination(*, is_enabled: bool = True) -> Destination:
    # Delivery never touches the database; an unsaved row is enough.
    return Destination(
        name="ops",
        kind=DestinationKind.SLACK_CHANNEL.value,
        channel_id="C1",
        is_enabled=is_enabled,
    )


@pytest.fixture
async def installed_slack(druks_db):
    bind_ambient_session(druks_db)
    row = await install_service(
        "slack",
        access_token=_BOT_TOKEN,
        identity={"client_id": "slack-client"},
        secrets={"client_secret": "slack-secret"},
    )
    await druks_db.commit()
    return row


@pytest.fixture
def slack(monkeypatch):
    """Slack's Web API, answered locally. Records each post. ``error`` makes Slack refuse."""
    fake = SimpleNamespace(posts=[], error="")

    async def api_call(client, api_method, *, params=None, json=None, **request):
        fake.posts.append({"bot_token": client.token, "method": api_method, **(json or {})})
        if fake.error:
            raise SlackApiError("refused", {"ok": False, "error": fake.error})
        return {"ok": True, "channel": "C1", "ts": "1.0"}

    monkeypatch.setattr(SlackClient, "api_call", api_call)
    return fake


async def test_create_get_list_delete_round_trip(druks_db):
    beta = await Destination.create(druks_db, name="beta", kind="slack_channel", channel_id="C1")
    await Destination.create(druks_db, name="alpha", kind="slack_channel", channel_id="C2")

    assert (await druks_db.get(Destination, beta.id)).id == beta.id
    assert (await Destination.get_for_name(druks_db, "alpha")).channel_id == "C2"
    assert await druks_db.get(Destination, "no-such-id") is None
    assert await Destination.get_for_name(druks_db, "no-such-name") is None
    assert [destination.name for destination in await Destination.list_all(druks_db)] == [
        "alpha",
        "beta",
    ]
    assert beta.is_enabled is True

    await beta.delete()
    assert await Destination.get_for_name(druks_db, "beta") is None
    assert [destination.name for destination in await Destination.list_all(druks_db)] == ["alpha"]


def _create_body(**overrides) -> dict:
    body = {"name": "ops", "kind": "slack_channel", "channelId": "C1"}
    body.update(overrides)
    return body


async def test_routes_create_posts_a_first_message_with_the_bot_token(
    tmp_path, installed_slack, slack
):
    async with asgi_client(configure_app_for_test(settings=make_settings(tmp_path))) as client:
        created = await client.post("/api/notifications/destinations", json=_create_body())

    assert created.status_code == 200
    assert created.json() == {
        "id": created.json()["id"],
        "name": "ops",
        "kind": "slack_channel",
        "channelId": "C1",
        "isEnabled": True,
    }
    (post,) = slack.posts
    assert (post["bot_token"], post["method"], post["channel"]) == (
        _BOT_TOKEN,
        "chat.postMessage",
        "C1",
    )


async def test_routes_refuse_a_channel_the_bot_cannot_post_to(tmp_path, installed_slack, slack):
    async with asgi_client(configure_app_for_test(settings=make_settings(tmp_path))) as client:
        for error, fix in (
            ("not_in_channel", "Invite the bot to the channel"),
            ("channel_not_found", "Check the channel ID"),
        ):
            slack.error = error
            created = await client.post("/api/notifications/destinations", json=_create_body())
            assert created.status_code == 422
            assert fix in created.json()["detail"]
        assert not (await client.get("/api/notifications/destinations")).json()


async def test_routes_refuse_a_destination_without_the_slack_card(tmp_path, druks_db, slack):
    async with asgi_client(configure_app_for_test(settings=make_settings(tmp_path))) as client:
        created = await client.post("/api/notifications/destinations", json=_create_body())

        assert created.status_code == 409
        assert "slack is not connected" in created.json()["detail"]
        assert not (await client.get("/api/notifications/destinations")).json()
    assert slack.posts == []


async def test_routes_reject_duplicate_name(tmp_path, installed_slack, slack):
    async with asgi_client(configure_app_for_test(settings=make_settings(tmp_path))) as client:
        first = await client.post("/api/notifications/destinations", json=_create_body())
        assert first.status_code == 200

        duplicate = await client.post("/api/notifications/destinations", json=_create_body())
        assert duplicate.status_code == 409


async def test_routes_reject_blank_name(tmp_path, druks_db):
    async with asgi_client(configure_app_for_test(settings=make_settings(tmp_path))) as client:
        for blank in ("", "   "):
            created = await client.post(
                "/api/notifications/destinations", json=_create_body(name=blank)
            )
            assert created.status_code == 422
        assert not (await client.get("/api/notifications/destinations")).json()


async def test_routes_toggle_enabled(tmp_path, installed_slack, slack):
    async with asgi_client(configure_app_for_test(settings=make_settings(tmp_path))) as client:
        destination_id = (
            await client.post("/api/notifications/destinations", json=_create_body())
        ).json()["id"]

        toggled = await client.patch(
            f"/api/notifications/destinations/{destination_id}", json={"is_enabled": False}
        )
        assert toggled.status_code == 200
        assert toggled.json()["isEnabled"] is False

        listed = (await client.get("/api/notifications/destinations")).json()
        assert listed[0]["isEnabled"] is False

        back_on = await client.patch(
            f"/api/notifications/destinations/{destination_id}", json={"is_enabled": True}
        )
        assert back_on.json()["isEnabled"] is True

        missing = await client.patch(
            "/api/notifications/destinations/no-such-id", json={"is_enabled": False}
        )
        assert missing.status_code == 404


async def test_routes_delete(tmp_path, installed_slack, slack):
    async with asgi_client(configure_app_for_test(settings=make_settings(tmp_path))) as client:
        destination_id = (
            await client.post("/api/notifications/destinations", json=_create_body())
        ).json()["id"]

        assert (
            await client.delete(f"/api/notifications/destinations/{destination_id}")
        ).status_code == 204
        assert not (await client.get("/api/notifications/destinations")).json()
        assert (
            await client.delete(f"/api/notifications/destinations/{destination_id}")
        ).status_code == 404


async def test_plain_delivery_posts_the_body_and_the_link(slack):
    client = SlackClient(token=_BOT_TOKEN)

    await deliver(
        client, _slack_destination(), "Review the PR", deep_link="https://github.com/acme/app/7"
    )

    (post,) = slack.posts
    assert (post["bot_token"], post["channel"]) == (_BOT_TOKEN, "C1")
    assert post["text"] == "Review the PR\nhttps://github.com/acme/app/7"
    (section,) = post["blocks"]
    assert section["text"]["text"] == post["text"]


async def test_button_delivery_posts_one_button_per_action(slack):
    actions = [{"id": "approve", "label": "Approve"}, {"id": "reject", "label": "Reject"}]

    await deliver(
        SlackClient(token=_BOT_TOKEN),
        _slack_destination(),
        "review the plan",
        actions=actions,
        token="tok_123",
    )

    (post,) = slack.posts
    assert post["text"] == "review the plan"
    section, action_block = post["blocks"]
    assert section["text"]["text"] == "review the plan"
    assert [button["text"]["text"] for button in action_block["elements"]] == [
        "Approve",
        "Reject",
    ]
    for button, action in zip(action_block["elements"], actions, strict=True):
        assert decode_button(button["action_id"]) == ("tok_123", action["id"])


async def test_delivery_to_a_channel_the_bot_is_not_in_says_to_invite_it(slack):
    slack.error = "not_in_channel"

    with pytest.raises(DeliveryError) as excinfo:
        await deliver(SlackClient(token=_BOT_TOKEN), _slack_destination(), "hello")

    assert "Invite the bot to the channel" in str(excinfo.value)
    # The Slack error keeps the request headers, and they carry the bot token.
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__suppress_context__ is True


async def test_a_refused_delivery_names_slack_s_reason(slack):
    slack.error = "is_archived"

    with pytest.raises(DeliveryError) as excinfo:
        await deliver(SlackClient(token=_BOT_TOKEN), _slack_destination(), "hello")

    assert str(excinfo.value) == "Delivery to destination 'ops' failed: is_archived."
    assert excinfo.value.__cause__ is None


async def test_a_delivery_slack_does_not_answer_names_the_failure(monkeypatch):
    async def api_call(client, api_method, **request):
        raise TimeoutError

    monkeypatch.setattr(SlackClient, "api_call", api_call)

    with pytest.raises(DeliveryError) as excinfo:
        await deliver(SlackClient(token=_BOT_TOKEN), _slack_destination(), "hello")

    assert str(excinfo.value) == "Delivery to destination 'ops' failed: TimeoutError."
    assert excinfo.value.__cause__ is None


async def test_disabled_destination_never_sends(slack):
    disabled = _slack_destination(is_enabled=False)

    with pytest.raises(DisabledDestinationError):
        await deliver(SlackClient(token=_BOT_TOKEN), disabled, "hello")

    assert slack.posts == []


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

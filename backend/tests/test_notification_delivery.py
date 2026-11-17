from types import SimpleNamespace

import pytest
from druks.core.apis.slack import SlackClient
from druks.notifications.buttons import decode_button, encode_button
from druks.notifications.delivery import deliver
from druks.notifications.exceptions import (
    ChannelUnavailableError,
    DeliveryError,
    MalformedButtonError,
)
from slack_sdk.errors import SlackApiError

_BOT_TOKEN = "xoxb-delivery"


@pytest.fixture
def slack(monkeypatch):
    """Slack's Web API, answered locally. Records each post. ``error`` makes Slack refuse."""
    fake = SimpleNamespace(posts=[], error="")

    async def api_call(client, api_method, *, params=None, json=None, **request):
        fake.posts.append({"bot_token": client.token, "method": api_method, **(json or {})})
        if fake.error:
            raise SlackApiError("refused", {"ok": False, "error": fake.error})
        # The SDK answers with a response object whose data is the parsed body.
        return SimpleNamespace(data={"ok": True, "channel": "C1", "ts": "1.0"})

    monkeypatch.setattr(SlackClient, "api_call", api_call)
    return fake


async def test_plain_delivery_posts_the_body_and_the_link(slack):
    client = SlackClient(token=_BOT_TOKEN)

    await deliver(client, "C1", "Review the PR", deep_link="https://github.com/acme/app/7")

    (post,) = slack.posts
    assert (post["bot_token"], post["channel"]) == (_BOT_TOKEN, "C1")
    assert post["text"] == "Review the PR\nhttps://github.com/acme/app/7"
    (section,) = post["blocks"]
    assert section["text"]["text"] == post["text"]


async def test_a_long_body_is_cut_to_fit_slack_and_keeps_its_link(slack):
    link = "https://druks.example/runs/run-1"

    await deliver(SlackClient(token=_BOT_TOKEN), "C1", "x" * 5000, deep_link=link)

    (post,) = slack.posts
    (section,) = post["blocks"]
    assert len(section["text"]["text"]) == 3000
    assert section["text"]["text"].endswith(f"…\n{link}")


async def test_button_delivery_posts_one_button_per_action(slack):
    actions = [{"id": "approve", "label": "Approve"}, {"id": "reject", "label": "Reject"}]

    await deliver(
        SlackClient(token=_BOT_TOKEN),
        "C1",
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

    with pytest.raises(ChannelUnavailableError) as excinfo:
        await deliver(SlackClient(token=_BOT_TOKEN), "C1", "hello")

    assert "Invite the bot to the channel" in str(excinfo.value)
    # The Slack error keeps the request headers, and they carry the bot token.
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__suppress_context__ is True


async def test_a_refused_delivery_names_slack_s_reason(slack):
    slack.error = "restricted_action"

    with pytest.raises(DeliveryError) as excinfo:
        await deliver(SlackClient(token=_BOT_TOKEN), "C1", "hello")

    assert str(excinfo.value) == "Delivery failed: restricted_action."
    assert excinfo.value.__cause__ is None


async def test_a_delivery_slack_does_not_answer_names_the_failure(monkeypatch):
    async def api_call(client, api_method, **request):
        raise TimeoutError

    monkeypatch.setattr(SlackClient, "api_call", api_call)

    with pytest.raises(DeliveryError) as excinfo:
        await deliver(SlackClient(token=_BOT_TOKEN), "C1", "hello")

    assert str(excinfo.value) == "Delivery failed: TimeoutError."
    assert excinfo.value.__cause__ is None


async def test_delivery_in_a_thread_replies_there(slack):
    await deliver(SlackClient(token=_BOT_TOKEN), "C1", "hello", thread_ts="1700000000.000100")

    (post,) = slack.posts
    assert (post["channel"], post["thread_ts"]) == ("C1", "1700000000.000100")


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


async def test_delivery_with_a_message_ts_edits_that_message_and_shows_the_outcome(slack):
    await deliver(
        SlackClient(token=_BOT_TOKEN),
        "C1",
        "review the plan",
        message_ts="1700000000.000200",
        outcome="Answer: Approve",
    )

    (post,) = slack.posts
    assert (post["method"], post["channel"], post["ts"]) == (
        "chat.update",
        "C1",
        "1700000000.000200",
    )
    assert [block["text"]["text"] for block in post["blocks"]] == [
        "review the plan",
        "Answer: Approve",
    ]


async def test_delivery_to_a_user_shows_the_message_to_that_person_only(slack):
    await deliver(
        SlackClient(token=_BOT_TOKEN),
        "C1",
        "This notification was already acknowledged.",
        user="U7",
        thread_ts="1700000000.000100",
    )

    (post,) = slack.posts
    assert (post["method"], post["channel"], post["user"], post["thread_ts"]) == (
        "chat.postEphemeral",
        "C1",
        "U7",
        "1700000000.000100",
    )

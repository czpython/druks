from types import SimpleNamespace

import pytest
from conftest import bind_ambient_session, install_service
from druks.accounts.models import Account
from druks.chat.channels.slack.channel import SlackChannel
from druks.chat.enums import ConversationSource
from druks.chat.models import Conversation
from druks.core.apis.slack import SlackClient
from druks.core.webhooks.slack import replace_buttons
from druks.durable.models import AgentCall, Artifact, Run
from druks.notifications.buttons import decode_button, encode_button
from druks.notifications.constants import CLOSED_MESSAGE, MARKDOWN_TEXT_LIMIT
from druks.notifications.delivery import cut_markdown, deliver
from druks.notifications.exceptions import (
    ChannelUnavailableError,
    DeliveryError,
    MalformedButtonError,
)
from druks.notifications.models import Notification
from druks.testing import seed_call, seed_run
from druks_field_notes.workflows import Summarize
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
    assert post["blocks"] == [{"type": "markdown", "text": post["text"]}]


async def test_a_body_too_long_for_slack_is_cut_and_keeps_its_link(slack):
    link = "https://druks.example/runs/run-1"
    body = "Your run failed: " + "x" * 20_000

    await deliver(SlackClient(token=_BOT_TOKEN), "C1", body, deep_link=link)

    (post,) = slack.posts
    assert len(post["text"]) == MARKDOWN_TEXT_LIMIT
    assert post["text"].startswith("Your run failed: xxx")
    assert post["text"].endswith(f"x\n\n…\n{link}")
    assert post["blocks"] == [{"type": "markdown", "text": post["text"]}]


async def test_a_long_reply_before_a_question_loses_its_end_and_the_question_stays(druks_db, slack):
    bind_ambient_session(druks_db)
    await install_service(
        "slack",
        access_token=_BOT_TOKEN,
        identity={"client_id": "c1"},
        secrets={"client_secret": "s"},
    )
    account = await Account.get_or_create(druks_db, "op@example.com")
    conversation = Conversation(
        account_id=account.id,
        source=ConversationSource.SLACK,
        user_id="U1",
        thread_id="C1:1700000000.000100",
    )
    druks_db.add(conversation)
    await druks_db.flush()
    reply = "I compared both.\n" * 1_000
    question = await Notification.create(
        druks_db,
        conversation_id=conversation.id,
        reason="question.asked",
        body=f"{reply}\n\nWhich database?",
        question_offset=len(reply),
        actions=[{"id": "1", "label": "Postgres"}, {"id": "2", "label": "MySQL"}],
    )

    await SlackChannel.post_buttons(druks_db, conversation, question)
    await replace_buttons(question.id, channel="C1", message_ts="1.0", outcome="<@U1> chose MySQL.")
    question.message_id = "C1:1.0"
    await SlackChannel.remove_buttons(druks_db, conversation, question)

    posted, clicked, closed = slack.posts
    for post in slack.posts:
        assert len(post["text"]) <= MARKDOWN_TEXT_LIMIT
        assert post["text"].endswith("I compared both.\n\n…\n\nWhich database?")
        assert post["blocks"][0] == {"type": "markdown", "text": post["text"]}
    assert [button["text"]["text"] for button in posted["blocks"][1]["elements"]] == [
        "Postgres",
        "MySQL",
    ]
    for edit, outcome in ((clicked, "<@U1> chose MySQL."), (closed, CLOSED_MESSAGE)):
        assert edit["blocks"][1:] == [
            {"type": "section", "text": {"type": "mrkdwn", "text": outcome}}
        ]


@pytest.mark.parametrize(
    ("text", "ending"),
    [
        ("Run this:\n```bash\n" + "make test\n" * 100, "make test\n```\n\n…"),
        ("1. Run this:\n   ```bash\n" + "   make test\n" * 100, "   make test\n   ```\n\n…"),
        (
            "````markdown\n```python\nprint()\n```\n" + "more of the example\n" * 100,
            "more of the example\n````\n\n…",
        ),
        ("```inline``` code\n" + "a line\n" * 100, "a line\n\n…"),
    ],
    ids=["fence", "fence-in-a-list-item", "fence-in-a-fence", "inline-code"],
)
def test_a_cut_closes_a_fence_only_as_markdown_closes_it(text, ending):
    assert cut_markdown(text, 300).endswith(ending)


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
    markdown, action_block = post["blocks"]
    assert markdown == {"type": "markdown", "text": "review the plan"}
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
    assert post["text"] == "review the plan"
    assert post["blocks"] == [
        {"type": "markdown", "text": "review the plan"},
        {"type": "section", "text": {"type": "mrkdwn", "text": "Answer: Approve"}},
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
    assert post["text"] == "This notification was already acknowledged."
    assert post["blocks"] == [{"type": "markdown", "text": post["text"]}]


async def _parked_review(druks_db, *, document: str = "", **ask) -> Run:
    """A run parked on an in-app review. Its call wrote ``document`` as the run's artifact."""
    run = await seed_run(
        druks_db,
        kind=Summarize.kind,
        state="parked",
        input_gate="review",
        input_request={
            "presentation": "in_app",
            "controls": [{"id": "approve", "note_required": False}],
            "questions": [],
            **ask,
        },
    )
    if document:
        call = await seed_call(druks_db, run, "summarize")
        await Artifact.record(
            druks_db,
            call_dir=call.call_dir,
            call_id=call.id,
            kind="markdown",
            title="Plan",
            content=document,
            event={},
        )
    return run


@pytest.mark.usefixtures("tmp_data_dir")
async def test_an_approval_writes_its_label_context_questions_and_document_in_order(druks_db):
    run = await _parked_review(
        druks_db,
        document="## Steps\nShip it.",
        context="The plan changes the schema.",
        questions=[
            {
                "id": "database",
                "prompt": "Which database?",
                "options": [
                    {"id": "postgres", "label": "Postgres", "recommended": True},
                    {"id": "mysql", "label": "MySQL", "recommended": False},
                ],
            },
            # An app's question can leave the flag out.
            {
                "id": "cache",
                "prompt": "Which cache?",
                "options": [{"id": "redis", "label": "Redis"}],
            },
        ],
    )

    rendered = await run.get_rendered_ask(has_chat=False)

    assert rendered["body"] == (
        "# Review: Plan\n\n"
        "The plan changes the schema.\n\n"
        "Which database?\n- Postgres (recommended)\n- MySQL\n\n"
        "Which cache?\n- Redis\n\n"
        "## Steps\nShip it."
    )


@pytest.mark.usefixtures("tmp_data_dir")
async def test_an_approval_whose_document_is_gone_goes_out_without_it(druks_db):
    run = await _parked_review(druks_db, document="Ship it.", context="Check the schema.")
    artifact = await Artifact.get_latest_for_run(druks_db, run.id)
    call = await AgentCall.get(druks_db, artifact.agent_call_id)
    (call.call_dir / artifact.path).unlink()

    rendered = await run.get_rendered_ask(has_chat=False)

    assert rendered["body"] == "# Review: Plan\n\nCheck the schema."

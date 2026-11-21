import asyncio
import os
from types import SimpleNamespace

import psycopg
import pytest
from conftest import install_service
from dbos import DBOS
from druks.accounts.context import current_conversation_id
from druks.accounts.models import Account
from druks.apps.registry import workflows
from druks.chat.channels.slack import channel as slack_channel
from druks.chat.enums import ConversationSource, MessageRole, MessageState
from druks.chat.models import Conversation, Message
from druks.core.services import Slack
from druks.core.webhooks import slack as slack_webhooks
from druks.database import configure_session, get_session
from druks.db import db_session
from druks.durable.constants import NOTIFICATIONS_QUEUE
from druks.durable.engine import configure_engine, init_dbos, launch, shutdown
from druks.durable.enums import RunState
from druks.models import StoredSubject
from druks.notifications import outbox
from druks.notifications.constants import CLOSE_QUESTION_WORKFLOW, CLOSED_MESSAGE
from druks.notifications.exceptions import (
    ChannelUnavailableError,
    DeliveryError,
    NotificationError,
)
from druks.notifications.models import Notification
from druks.notifications.outbox import send_notification
from druks.notifications.services import respond_to_notification
from druks.secrets.datastructures import Audience
from druks.secrets.models import VaultSecret
from druks.settings import Settings, configure_settings, get_settings
from druks.testing import configure_app_for_test, init_db, make_settings
from druks.workflows import Gate, OperatorReply, Run, Workflow
from fastapi.testclient import TestClient
from pydantic import BaseModel, Field
from sqlalchemy import NullPool, create_engine, select, text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.orm import Session

PG_BASE = os.environ.get("DRUKS_TEST_PG", "postgresql://druks:druks@localhost:5432")
DB = "druks_notifications_durable_test"
URL = f"{PG_BASE.replace('postgresql://', 'postgresql+psycopg://')}/{DB}"
_BOT_TOKEN = "xoxb-durablesecret"


def _pg_up() -> bool:
    try:
        psycopg.connect(f"{PG_BASE}/postgres", connect_timeout=2).close()
        return True
    except psycopg.Error:
        return False


pytestmark = [
    pytest.mark.skipif(not _pg_up(), reason="test Postgres not reachable"),
    pytest.mark.asyncio(loop_scope="module"),
]


class _Question(BaseModel):
    id: str
    prompt: str
    options: list[dict] = Field(default_factory=list)


class NotificationProbe(StoredSubject):
    __tablename__ = "test_notification_probes"


# What the in-app reviews resumed with — the respond round-trip asserts the
# payload arrived in the workflow verbatim.
_REVIEW_REPLIES: list[OperatorReply] = []
# The run ids of every FailingFlow execution, so a test sees a replay happen.
_FAILED_EXECUTIONS: list[str] = []


def _build_park_flows():
    class ParkNote(Gate):
        name = "park_note"
        action: str = ""

        @classmethod
        async def on_wait(cls, workflow: Workflow) -> None:
            # Overridden so a subjectless park is allowed (the AC5 case).
            return

    class InAppFlow(Workflow):
        subject = NotificationProbe

        async def run_multistep(self) -> None:
            reply = await self.review(
                questions=[
                    _Question(
                        id="q1",
                        prompt="Which database?",
                        options=[{"id": "postgres", "label": "Postgres"}],
                    )
                ]
            )
            _REVIEW_REPLIES.append(reply)

    class ExternalFlow(Workflow):
        subject = NotificationProbe

        async def run_multistep(self) -> None:
            await ParkNote.wait(
                input_request={"presentation": "external", "label": "Answer on the ticket"}
            )

    class SubjectlessFlow(Workflow):
        async def run_multistep(self) -> None:
            await ParkNote.wait(
                input_request={"presentation": "external", "label": "Answer on the ticket"}
            )

    class ExternalUrlFlow(Workflow):
        subject = NotificationProbe

        async def run_multistep(self) -> None:
            await ParkNote.wait(
                input_request={
                    "presentation": "external",
                    "label": "Review the PR",
                    "url": "https://github.com/acme/app/pull/7",
                }
            )

    class DoubleParkFlow(Workflow):
        subject = NotificationProbe

        async def run_multistep(self) -> None:
            await ParkNote.wait(input_request={"presentation": "external", "label": "Round one"})
            await ParkNote.wait(input_request={"presentation": "external", "label": "Round two"})

    class FailingFlow(Workflow):
        subject = NotificationProbe

        async def run_multistep(self) -> None:
            _FAILED_EXECUTIONS.append(self.workflow_id)
            raise RuntimeError("the disk is full")

    return InAppFlow, ExternalFlow, SubjectlessFlow, ExternalUrlFlow, DoubleParkFlow, FailingFlow


@pytest.fixture(scope="module", autouse=True)
async def rt():
    original_settings = get_settings()

    admin = psycopg.connect(f"{PG_BASE}/postgres", autocommit=True)
    admin.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
    admin.execute(f"CREATE DATABASE {DB}")
    admin.close()

    schema_engine = create_engine(URL)
    init_db(schema_engine)
    with Session(schema_engine) as session:
        session.add(Account(username="op@example.com", is_default=True))
        session.add_all(
            NotificationProbe(id=subject_id)
            for subject_id in (
                9001,
                9002,
                9003,
                9004,
                9005,
                9006,
                9007,
                9008,
                9009,
                9010,
                9014,
                9015,
                9016,
                9017,
                9018,
            )
        )
        session.commit()
    # NullPool: the TestClient below runs requests on its own loop, and a pooled
    # async connection must never cross loops.
    engine = create_async_engine(URL, poolclass=NullPool)
    configure_engine(engine)
    configure_session(engine)
    (
        in_app_flow,
        external_flow,
        subjectless_flow,
        external_url_flow,
        double_park_flow,
        failing_flow,
    ) = _build_park_flows()
    configure_settings(Settings(database_url=URL))  # pyright: ignore[reportCallIssue]
    # The outbox module was imported above — its queue + workflow register
    # before launch(), which is the wiring this whole module runs through.
    init_dbos()
    await launch()
    try:
        yield SimpleNamespace(
            engine=engine,
            schema_engine=schema_engine,
            InAppFlow=in_app_flow,
            ExternalFlow=external_flow,
            SubjectlessFlow=subjectless_flow,
            ExternalUrlFlow=external_url_flow,
            DoubleParkFlow=double_park_flow,
            FailingFlow=failing_flow,
        )
    finally:
        shutdown()
        await engine.dispose()
        schema_engine.dispose()
        for kind in (
            "in_app_flow",
            "external_flow",
            "subjectless_flow",
            "external_url_flow",
            "double_park_flow",
            "failing_flow",
        ):
            workflows._items.pop(kind, None)
        configure_settings(original_settings)


class _DeliverSpy:
    def __init__(self):
        self.calls: list[dict] = []
        self.failures_remaining = 0
        self.always_fail = False
        self.error: Exception = DeliveryError("is_archived")
        # Set, a post waits for it, so a test can act while the post is under way.
        self.hold: asyncio.Event | None = None

    async def __call__(
        self,
        client,
        channel,
        body,
        *,
        thread_ts="",
        message_ts="",
        user="",
        actions=None,
        token=None,
        deep_link=None,
        outcome="",
        question_offset=None,
    ):
        self.calls.append(
            {
                "bot_token": client.token,
                "channel": channel,
                "thread_ts": thread_ts,
                "message_ts": message_ts,
                "user": user,
                "body": body,
                "actions": actions,
                "token": token,
                "deep_link": deep_link,
                "outcome": outcome,
                "question_offset": question_offset,
            }
        )
        if self.hold and not message_ts:
            await self.hold.wait()
        if self.always_fail:
            raise self.error
        if self.failures_remaining:
            self.failures_remaining -= 1
            raise self.error
        return {"ok": True, "channel": channel, "ts": f"1700000000.{len(self.calls):06d}"}


@pytest.fixture
async def deliver_spy(monkeypatch):
    spy = _DeliverSpy()
    monkeypatch.setattr(outbox, "deliver", spy)
    monkeypatch.setattr(slack_channel, "deliver_notification", spy)
    monkeypatch.setattr(slack_webhooks, "deliver", spy)
    return spy


async def _seed(rt, seeder):
    # Commit for real: the outbox worker reads through its own sessions.
    # Expunge first so the returned instance keeps its loaded attributes past
    # the commit's expiry.
    session = get_session(rt.engine)
    db_session.registry.set(session)
    try:
        result = await seeder()
        await session.flush()
        session.expunge_all()
        await session.commit()
        return result
    finally:
        await db_session.remove()
        await session.close()


async def _install_slack():
    # The module shares one database, and a service holds one live grant.
    if not await Slack.get_grant():
        await install_service(
            "slack",
            access_token=_BOT_TOKEN,
            identity={"client_id": "slack-client"},
            secrets={"client_secret": "slack-secret"},
        )


async def _seed_person(rt, name):
    # The module shares one database, so each test names its own person.
    async def create():
        await _install_slack()
        account = await Account.get_or_create(db_session(), f"{name}@example.com")
        await account.update_preferences(notification_channel_id="C1")
        return account

    return await _seed(rt, create)


async def _deliver(rt, *, to, subject=None, reason="r", body="b", actions=None):
    # The create-seam path a producer uses (the gate-park producer's shape):
    # persist the row committed, then enqueue the outbox — no notify() hatch.
    async def create():
        account = await Account.get_or_create(db_session(), f"{to}@example.com")
        notification = await Notification.create(
            db_session(),
            account_id=account.id,
            reason=reason,
            body=body,
            subject=subject or {"type": "notification_probe", "id": 1},
            actions=actions,
        )
        return notification.id

    notification_id = await _seed(rt, create)
    await DBOS.enqueue_workflow_async(NOTIFICATIONS_QUEUE, send_notification, notification_id)
    return notification_id


async def _snapshot(rt, notification_id) -> dict:
    session = get_session(rt.engine)
    try:
        notification = await session.get(Notification, notification_id)
        return {
            "state": notification.state,
            "attempts": notification.attempts,
            "last_error": notification.last_error,
            "delivered_at": notification.delivered_at,
            "token": notification.correlation_token,
            "message_id": notification.message_id,
        }
    finally:
        await session.close()


async def _wait_for(rt, notification_id, predicate, timeout=30.0):
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        snapshot = await _snapshot(rt, notification_id)
        if predicate(snapshot):
            return snapshot
        await asyncio.sleep(0.1)
    raise AssertionError(f"timed out; last={await _snapshot(rt, notification_id)}")


async def test_outbox_delivers_with_the_bot_token_actions_and_token(rt, deliver_spy):
    await _seed_person(rt, "happy")

    notification_id = await _deliver(
        rt, to="happy", reason="ops.alert", body="hello", actions=[{"id": "ok", "label": "OK"}]
    )

    done = await _wait_for(rt, notification_id, lambda s: s["state"] == "delivered")
    assert done["attempts"] == 1
    assert done["delivered_at"] is not None
    assert done["message_id"] == "C1:1700000000.000001"
    (call,) = deliver_spy.calls
    assert call["bot_token"] == _BOT_TOKEN
    assert (call["channel"], call["thread_ts"]) == ("C1", "")
    assert call["body"] == "hello"
    assert call["actions"] == [{"id": "ok", "label": "OK"}]
    assert call["token"] == done["token"]


async def test_what_a_click_starts_reaches_slack_with_the_bot_token(rt, deliver_spy):
    await _seed_person(rt, "clicked")
    notification_id = await _deliver(rt, to="clicked", body="review the plan")
    await _wait_for(rt, notification_id, lambda s: s["state"] == "delivered")

    await slack_webhooks.replace_buttons(
        notification_id,
        channel="C1",
        message_ts="1700000000.000200",
        outcome="Answer: Approve",
    )
    await slack_webhooks.explain_reason(
        "C1",
        user="U7",
        reason="This notification was already acknowledged.",
        thread_ts="",
    )

    _, edited, explained = deliver_spy.calls
    assert (edited["bot_token"], explained["bot_token"]) == (_BOT_TOKEN, _BOT_TOKEN)
    assert (edited["body"], edited["message_ts"], edited["outcome"]) == (
        "review the plan",
        "1700000000.000200",
        "Answer: Approve",
    )
    assert (explained["body"], explained["user"]) == (
        "This notification was already acknowledged.",
        "U7",
    )


async def test_transient_failure_retries_to_delivered_and_touches_no_run(rt, deliver_spy):
    deliver_spy.failures_remaining = 2
    await _seed_person(rt, "flaky")

    notification_id = await _deliver(rt, to="flaky")

    done = await _wait_for(rt, notification_id, lambda s: s["state"] == "delivered")
    assert done["attempts"] == 3
    assert len(deliver_spy.calls) == 3
    # Delivery is decoupled from the run lifecycle: no Run row exists or was touched.
    with rt.schema_engine.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM durable_runs")).scalar_one() == 0


async def test_terminal_failure_marks_failed_sanitized_and_reads_back(
    rt, deliver_spy, monkeypatch, tmp_path
):
    deliver_spy.always_fail = True
    deliver_spy.error = DeliveryError("is_archived")
    # Two attempts instead of five: same terminal path, fraction of the backoff.
    monkeypatch.setattr(outbox, "_SEND_RETRIES", {"retries_allowed": True, "max_attempts": 2})
    await _seed_person(rt, "doomed")

    notification_id = await _deliver(rt, to="doomed")

    done = await _wait_for(rt, notification_id, lambda s: s["state"] == "failed")
    assert done["attempts"] == 2
    assert len(deliver_spy.calls) == 2
    assert done["last_error"] == "Delivery failed: is_archived."

    settings = make_settings(tmp_path, identity={"mode": "header", "header": "X-ExeDev-Email"})
    app = configure_app_for_test(settings=settings, engine=rt.engine, authenticated=False)
    shown = TestClient(app).get(
        f"/api/notifications/{notification_id}", headers={"X-ExeDev-Email": "doomed@example.com"}
    )
    assert shown.status_code == 200
    assert shown.json()["state"] == "failed"
    assert shown.json()["attempts"] == 2
    assert done["token"] not in shown.text


async def test_unexpected_error_reduces_to_class_name(rt, deliver_spy, monkeypatch):
    # A misbehaving transport that leaks the bot token in its message must never
    # reach last_error verbatim.
    deliver_spy.always_fail = True
    deliver_spy.error = RuntimeError(f"boom at {_BOT_TOKEN}")
    monkeypatch.setattr(outbox, "_SEND_RETRIES", {"retries_allowed": True, "max_attempts": 2})
    await _seed_person(rt, "leaky")

    notification_id = await _deliver(rt, to="leaky")

    done = await _wait_for(rt, notification_id, lambda s: s["state"] == "failed")
    assert done["last_error"] == "RuntimeError"


async def test_a_send_without_slack_records_that_slack_is_not_connected(
    rt, deliver_spy, monkeypatch
):
    monkeypatch.setattr(outbox, "_SEND_RETRIES", {"retries_allowed": True, "max_attempts": 2})
    await _seed_person(rt, "unplugged")

    async def revoke_grant():
        grant = await Slack.get_grant()
        await grant.revoke("user")

    await _seed(rt, revoke_grant)

    notification_id = await _deliver(rt, to="unplugged")

    done = await _wait_for(rt, notification_id, lambda s: s["state"] == "failed")
    assert "slack is not connected" in done["last_error"]
    assert deliver_spy.calls == []


async def _notify_person(rt, email, *, slack_user="", notification_channel=""):
    # The module shares one database, so each test names its own person.
    async def create():
        await _install_slack()
        account = await Account.get_or_create(db_session(), email)
        if slack_user:
            await VaultSecret.connect(
                db_session(),
                Audience.service("slack"),
                account_id=account.id,
                refresh_token="",
                scopes=["users:read"],
                identity={"authority": "https://slack.com/T1", "subject": slack_user},
            )
        if notification_channel:
            await account.update_preferences(notification_channel_id=notification_channel)
        notification = await Notification.create(
            db_session(),
            account_id=account.id,
            reason="subscription.disconnected",
            body="Your Anthropic subscription is disconnected.",
            deep_link="https://druks.test/settings/providers",
        )
        return notification.id

    notification_id = await _seed(rt, create)
    await DBOS.enqueue_workflow_async(NOTIFICATIONS_QUEUE, send_notification, notification_id)
    return notification_id


async def test_a_person_s_notification_goes_to_their_slack_user(rt, deliver_spy):
    notification_id = await _notify_person(rt, "dm@example.com", slack_user="U42")

    done = await _wait_for(rt, notification_id, lambda s: s["state"] == "delivered")
    (call,) = deliver_spy.calls
    assert call["channel"] == "U42"
    assert call["deep_link"] == "https://druks.test/settings/providers"
    assert done["message_id"] == "U42:1700000000.000001"


async def test_a_person_s_notification_goes_to_the_channel_they_chose(rt, deliver_spy):
    notification_id = await _notify_person(
        rt, "channel@example.com", slack_user="U43", notification_channel="C7"
    )

    await _wait_for(rt, notification_id, lambda s: s["state"] == "delivered")
    (call,) = deliver_spy.calls
    assert call["channel"] == "C7"


async def test_a_channel_that_refuses_the_bot_sends_the_notification_by_dm(rt, deliver_spy):
    deliver_spy.failures_remaining = 1
    deliver_spy.error = ChannelUnavailableError("the bot is not in channel C8")
    notification_id = await _notify_person(
        rt, "removed@example.com", slack_user="U44", notification_channel="C8"
    )

    done = await _wait_for(rt, notification_id, lambda s: s["state"] == "delivered")
    assert [call["channel"] for call in deliver_spy.calls] == ["C8", "U44"]
    assert done["message_id"].startswith("U44:")


async def test_a_notification_with_no_channel_is_delivered_with_no_message(rt, deliver_spy):
    notification_id = await _notify_person(rt, "no-slack@example.com")

    done = await _wait_for(rt, notification_id, lambda s: s["state"] == "delivered")
    assert done["attempts"] == 1
    assert deliver_spy.calls == []


async def test_rerun_on_delivered_notification_skips_the_send(rt, deliver_spy):
    await _seed_person(rt, "once")
    notification_id = await _deliver(rt, to="once")
    await _wait_for(rt, notification_id, lambda s: s["state"] == "delivered")
    assert len(deliver_spy.calls) == 1

    handle = await DBOS.enqueue_workflow_async(
        NOTIFICATIONS_QUEUE, send_notification, notification_id
    )
    await handle.get_result()

    assert len(deliver_spy.calls) == 1


async def test_create_seam_plus_direct_enqueue_delivers(rt, deliver_spy):
    account = await _seed_person(rt, "seam")

    async def create():
        return await Notification.create(
            db_session(),
            account_id=account.id,
            reason="r",
            body="b",
            subject={"type": "probe", "id": 1},
        )

    notification = await _seed(rt, create)
    # The create seam persisted a pending row and enqueued nothing.
    assert (await _snapshot(rt, notification.id))["state"] == "pending"
    assert deliver_spy.calls == []

    handle = await DBOS.enqueue_workflow_async(
        NOTIFICATIONS_QUEUE, send_notification, notification.id
    )
    await handle.get_result()

    assert (await _snapshot(rt, notification.id))["state"] == "delivered"
    assert len(deliver_spy.calls) == 1


async def _set_notification_channel(rt, channel_id):
    async def update():
        await _install_slack()
        account = await Account.get_default(db_session())
        await account.update_preferences(notification_channel_id=channel_id)

    await _seed(rt, update)


async def _run_snapshot(rt, workflow_id) -> Run:
    session = get_session(rt.engine)
    try:
        return await session.get(Run, workflow_id)
    finally:
        await session.close()


async def _notifications_for_run(rt, workflow_id) -> list[Notification]:
    session = get_session(rt.engine)
    try:
        result = await session.execute(
            select(Notification).where(Notification.run_id == workflow_id).order_by(Notification.id)
        )
        return list(result.scalars().all())
    finally:
        await session.close()


async def _wait_run(rt, workflow_id, predicate, timeout=30.0) -> Run:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        run = await _run_snapshot(rt, workflow_id)
        if run and predicate(run):
            return run

        await asyncio.sleep(0.1)
    raise AssertionError(f"timed out; last={await _run_snapshot(rt, workflow_id)}")


async def _wait_notification(rt, workflow_id, state, timeout=30.0) -> Notification:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        rows = await _notifications_for_run(rt, workflow_id)
        if rows and rows[0].state == state:
            return rows[0]
        await asyncio.sleep(0.1)
    raise AssertionError(f"timed out; last={await _notifications_for_run(rt, workflow_id)}")


async def test_in_app_park_notifies_with_actions(rt, deliver_spy):
    await _set_notification_channel(rt, "C-inbox-inapp")
    settings = get_settings()
    urls = settings.urls.model_copy(update={"endpoint": "https://druks.example"})
    configure_settings(settings.model_copy(update={"urls": urls}))

    workflow_id = await rt.InAppFlow.start(subject=NotificationProbe(id=9001))
    parked = await _wait_run(rt, workflow_id, lambda run: run.state == RunState.PARKED)

    notification = await _wait_notification(rt, workflow_id, "delivered")
    assert len(await _notifications_for_run(rt, workflow_id)) == 1
    assert notification.reason == "gate.parked"
    assert notification.body.startswith("# Review")
    assert "Which database?\n- Postgres" in notification.body
    # A click sends only its control: a bare request_changes or an option is refused.
    assert notification.actions == [{"id": "approve", "label": "Approve"}]
    assert notification.deep_link == f"https://druks.example/runs/{workflow_id}"
    assert notification.subject == {"type": "notification_probe", "id": 9001}
    assert notification.run_id == workflow_id
    assert notification.run_parked_at == parked.input_requested_at

    (call,) = deliver_spy.calls
    assert call["actions"] == notification.actions
    assert call["token"] == notification.correlation_token
    assert call["deep_link"] == notification.deep_link


async def test_external_park_notifies_without_actions(rt, deliver_spy):
    await _set_notification_channel(rt, "C-inbox-ext")

    workflow_id = await rt.ExternalFlow.start(subject=NotificationProbe(id=9002))
    await _wait_run(rt, workflow_id, lambda run: run.state == RunState.PARKED)

    notification = await _wait_notification(rt, workflow_id, "delivered")
    assert notification.body == "Answer on the ticket"
    assert notification.actions is None
    assert notification.deep_link is None
    (call,) = deliver_spy.calls
    assert call["actions"] is None


async def test_external_park_with_declared_url_sets_deep_link(rt, deliver_spy):
    await _set_notification_channel(rt, "C-inbox-url")
    settings = get_settings()
    urls = settings.urls.model_copy(update={"endpoint": "https://druks.example"})
    configure_settings(settings.model_copy(update={"urls": urls}))

    workflow_id = await rt.ExternalUrlFlow.start(subject=NotificationProbe(id=9003))
    await _wait_run(rt, workflow_id, lambda run: run.state == RunState.PARKED)

    notification = await _wait_notification(rt, workflow_id, "delivered")
    assert notification.deep_link == "https://github.com/acme/app/pull/7"
    assert notification.actions is None
    (call,) = deliver_spy.calls
    assert call["deep_link"] == notification.deep_link


async def test_a_park_with_no_channel_is_delivered_with_no_message(rt, deliver_spy):
    await _set_notification_channel(rt, None)

    workflow_id = await rt.ExternalFlow.start(subject=NotificationProbe(id=9004))
    await _wait_run(rt, workflow_id, lambda run: run.state == RunState.PARKED)

    # The unattended account has no channel and no Slack sign-in.
    await _wait_notification(rt, workflow_id, "delivered")
    assert deliver_spy.calls == []


async def test_subjectless_park_notifies_nothing(rt, deliver_spy):
    await _set_notification_channel(rt, "C-inbox-nosubj")

    workflow_id = await rt.SubjectlessFlow.start(subject=None)
    await _wait_run(rt, workflow_id, lambda run: run.state == RunState.PARKED)

    await asyncio.sleep(1.0)
    assert await _notifications_for_run(rt, workflow_id) == []
    assert deliver_spy.calls == []


async def test_failed_delivery_leaves_run_parked_and_resumable(rt, deliver_spy, monkeypatch):
    deliver_spy.always_fail = True
    monkeypatch.setattr(outbox, "_SEND_RETRIES", {"retries_allowed": True, "max_attempts": 2})
    await _set_notification_channel(rt, "C-inbox-flaky")

    workflow_id = await rt.InAppFlow.start(subject=NotificationProbe(id=9006))
    await _wait_run(rt, workflow_id, lambda run: run.state == RunState.PARKED)
    await _wait_notification(rt, workflow_id, "failed")

    # The park never noticed the dead endpoint: still waiting, still resumable.
    parked = await _run_snapshot(rt, workflow_id)
    assert parked.state == RunState.PARKED
    await parked.resume(action="approve")
    await _wait_run(rt, workflow_id, lambda run: run.state == RunState.FINISHED)


async def test_replayed_park_notifies_once(rt, deliver_spy):
    await _set_notification_channel(rt, "C-inbox-replay")

    workflow_id = await rt.ExternalFlow.start(subject=NotificationProbe(id=9007))
    await _wait_run(rt, workflow_id, lambda run: run.state == RunState.PARKED)
    await _wait_notification(rt, workflow_id, "delivered")
    assert len(deliver_spy.calls) == 1

    def _execution_claim():
        with rt.schema_engine.connect() as connection:
            return connection.execute(
                text(
                    "SELECT started_at_epoch_ms FROM dbos.workflow_status WHERE workflow_uuid = :id"
                ),
                {"id": workflow_id},
            ).scalar_one()

    # Re-execute the parked workflow from its checkpoints: the notify step
    # is memoized (same row) and the enqueue is a recorded child-start (no
    # second outbox run). Resume clears the executor claim; a fresh stamp
    # proves the replay really re-executed rather than no-opping.
    first_claim = _execution_claim()
    await DBOS.resume_workflow_async(workflow_id)
    deadline = asyncio.get_event_loop().time() + 15
    while asyncio.get_event_loop().time() < deadline:
        claim = _execution_claim()
        if claim and claim != first_claim:
            break
        await asyncio.sleep(0.1)
    else:
        raise AssertionError("workflow was never re-executed after resume")
    await _wait_run(rt, workflow_id, lambda run: run.state == RunState.PARKED)
    await asyncio.sleep(1.0)  # room for a wrong duplicate enqueue to land

    assert len(await _notifications_for_run(rt, workflow_id)) == 1
    assert len(deliver_spy.calls) == 1

    # Two recv waiters now share the topic (the pre-resume one and the
    # replayed one); cancel ends the run and both.
    session = get_session(rt.engine)
    db_session.registry.set(session)
    try:
        run = await session.get(Run, workflow_id)
        await run.cancel()
        await session.commit()
    finally:
        await db_session.remove()
        await session.close()


async def test_each_park_round_gets_its_own_notification(rt, deliver_spy):
    await _set_notification_channel(rt, "C-inbox-rounds")

    workflow_id = await rt.DoubleParkFlow.start(subject=NotificationProbe(id=9008))
    first_round = await _wait_run(rt, workflow_id, lambda run: run.state == RunState.PARKED)
    await _wait_notification(rt, workflow_id, "delivered")
    assert len(deliver_spy.calls) == 1

    await first_round.resume(action="go")
    # The same code path parks again — a later step invocation, so a new
    # checkpoint, a new row, a new delivery.
    await _wait_run(
        rt,
        workflow_id,
        lambda run: (
            run.state == RunState.PARKED
            and run.input_requested_at != first_round.input_requested_at
        ),
    )
    deadline = asyncio.get_event_loop().time() + 30
    while asyncio.get_event_loop().time() < deadline:
        rows = await _notifications_for_run(rt, workflow_id)
        if len(rows) == 2 and rows[1].state == "delivered":
            break
        await asyncio.sleep(0.1)
    else:
        raise AssertionError(f"second round never notified; {deliver_spy.calls=}")

    rows = await _notifications_for_run(rt, workflow_id)
    assert [row.body for row in rows] == ["Round one", "Round two"]
    assert rows[0].run_parked_at != rows[1].run_parked_at
    assert len(deliver_spy.calls) == 2


async def test_a_failed_run_with_no_chat_tells_its_owner(rt, deliver_spy):
    await _set_notification_channel(rt, None)
    settings = get_settings()
    urls = settings.urls.model_copy(update={"endpoint": "https://druks.example"})
    configure_settings(settings.model_copy(update={"urls": urls}))
    workflow_id = await rt.FailingFlow.start(subject=NotificationProbe(id=9016))
    run = await _wait_run(rt, workflow_id, lambda run: run.state == RunState.FAILED)

    (notification,) = await _notifications_for_run(rt, workflow_id)
    assert notification.account_id == run.account_id
    assert notification.reason == "run.failed"
    assert "the disk is full" in notification.body
    assert notification.deep_link == f"https://druks.example/runs/{workflow_id}"
    # The unattended account has no Slack sign-in, so the bell is where it reads this.
    await _wait_for(rt, notification.id, lambda s: s["state"] == "delivered")


async def test_a_failed_run_a_chat_started_sends_no_notification(rt, deliver_spy):
    async def create():
        account = await Account.get_default(db_session())
        conversation = Conversation(
            account_id=account.id,
            source=ConversationSource.SLACK,
            user_id="U1",
            thread_id="C9:1700000000.000200",
        )
        db_session().add(conversation)
        await db_session().flush()
        return conversation.id

    reset = current_conversation_id.set(await _seed(rt, create))
    try:
        workflow_id = await rt.FailingFlow.start(subject=NotificationProbe(id=9017))
    finally:
        current_conversation_id.reset(reset)
    await _wait_run(rt, workflow_id, lambda run: run.state == RunState.FAILED)

    assert await _notifications_for_run(rt, workflow_id) == []


async def test_a_replayed_failure_tells_the_owner_once(rt, deliver_spy):
    workflow_id = await rt.FailingFlow.start(subject=NotificationProbe(id=9018))
    await _wait_run(rt, workflow_id, lambda run: run.state == RunState.FAILED)

    # A crash after the notification's checkpoints, before DBOS recorded the ERROR, leaves
    # the workflow PENDING. DBOS never resumes an ERROR workflow, so put it back there.
    with rt.schema_engine.begin() as connection:
        connection.execute(
            text("UPDATE dbos.workflow_status SET status = 'PENDING' WHERE workflow_uuid = :id"),
            {"id": workflow_id},
        )
    await DBOS.resume_workflow_async(workflow_id)
    deadline = asyncio.get_event_loop().time() + 15
    while _FAILED_EXECUTIONS.count(workflow_id) < 2:
        if asyncio.get_event_loop().time() > deadline:
            raise AssertionError("workflow was never re-executed after resume")
        await asyncio.sleep(0.1)
    await _wait_run(rt, workflow_id, lambda run: run.state == RunState.FAILED)
    await asyncio.sleep(1.0)  # room for a wrong duplicate enqueue to land

    assert len(await _notifications_for_run(rt, workflow_id)) == 1


async def test_an_answer_from_elsewhere_closes_the_question_s_message(rt, deliver_spy):
    await _set_notification_channel(rt, "C-inbox-answered")

    workflow_id = await rt.InAppFlow.start(subject=NotificationProbe(id=9020))
    parked = await _wait_run(rt, workflow_id, lambda run: run.state == RunState.PARKED)
    posted = await _wait_notification(rt, workflow_id, "delivered")

    await parked.resume(action="approve", answers={"q1": "postgres"}, note="")
    await _wait_notification(rt, workflow_id, "acknowledged")

    (edit,) = [call for call in deliver_spy.calls if call["outcome"]]
    assert f"{edit['channel']}:{edit['message_ts']}" == posted.message_id
    assert edit["outcome"] == CLOSED_MESSAGE


async def test_a_cancelled_run_closes_the_question_s_message(rt, deliver_spy):
    await _set_notification_channel(rt, "C-inbox-cancelled")

    workflow_id = await rt.InAppFlow.start(subject=NotificationProbe(id=9021))
    await _wait_run(rt, workflow_id, lambda run: run.state == RunState.PARKED)
    posted = await _wait_notification(rt, workflow_id, "delivered")

    session = get_session(rt.engine)
    db_session.registry.set(session)
    try:
        await (await session.get(Run, workflow_id)).cancel()
        await session.commit()
    finally:
        await db_session.remove()
        await session.close()
    await _wait_notification(rt, workflow_id, "acknowledged")

    (edit,) = [call for call in deliver_spy.calls if call["outcome"]]
    assert f"{edit['channel']}:{edit['message_ts']}" == posted.message_id
    assert edit["outcome"] == CLOSED_MESSAGE


async def _respond_in_own_session(rt, token, choice, *, user_id=None):
    # Each caller is its own task, so the scoped registry gives each its own
    # session — a real request-shaped transaction. (The HTTP layer itself is
    # pinned by the unit route tests; driving DBOS's send through a TestClient
    # portal loop here would poison the module's shared executor.)
    session = get_session(rt.engine)
    db_session.registry.set(session)
    try:
        await respond_to_notification(session, token, choice, user_id=user_id, account_id=None)
        await session.commit()
        return "ok"
    except NotificationError as error:
        await session.rollback()
        return type(error).__name__
    finally:
        await db_session.remove()
        await session.close()


def _dbos_replies(rt, workflow_id) -> int:
    with rt.schema_engine.connect() as connection:
        return connection.execute(
            text(
                "SELECT count(*) FROM dbos.notifications"
                " WHERE destination_uuid = :id AND topic = 'review'"
            ),
            {"id": workflow_id},
        ).scalar_one()


async def test_respond_round_trip_finishes_the_run(rt, deliver_spy):
    await _set_notification_channel(rt, "C-inbox-respond")
    workflow_id = await rt.InAppFlow.start(subject=NotificationProbe(id=9009))
    await _wait_run(rt, workflow_id, lambda run: run.state == RunState.PARKED)
    notification = await _wait_notification(rt, workflow_id, "delivered")
    token = notification.correlation_token
    assert notification.deep_link is None

    first = await _respond_in_own_session(
        rt, token, {"control": "approve", "answers": {"q1": "postgres"}, "note": ""}
    )
    assert first == "ok"

    await _wait_run(rt, workflow_id, lambda run: run.state == RunState.FINISHED)
    assert OperatorReply(action="approve", answers={"q1": "postgres"}) in _REVIEW_REPLIES
    assert (await _notifications_for_run(rt, workflow_id))[0].state == "acknowledged"

    # Sequential second answer: the acknowledged fast-path rejects it, and the
    # round's DBOS bookkeeping still holds exactly one reply.
    second = await _respond_in_own_session(rt, token, {"control": "approve"})
    assert second == "AlreadyAcknowledgedError"
    assert _dbos_replies(rt, workflow_id) == 1


async def test_a_slack_chat_s_review_offers_its_buttons_in_its_thread(rt, deliver_spy):
    settings = get_settings()
    urls = settings.urls.model_copy(update={"endpoint": "https://druks.example"})
    configure_settings(settings.model_copy(update={"urls": urls}))

    async def create():
        await _install_slack()
        account = await Account.get_default(db_session())
        conversation = Conversation(
            account_id=account.id,
            source=ConversationSource.SLACK,
            user_id="U1",
            thread_id="C9:1700000000.000100",
        )
        db_session().add(conversation)
        await db_session().flush()
        return conversation.id

    conversation_id = await _seed(rt, create)
    reset = current_conversation_id.set(conversation_id)
    try:
        workflow_id = await rt.InAppFlow.start(subject=NotificationProbe(id=9015))
    finally:
        current_conversation_id.reset(reset)
    await _wait_run(rt, workflow_id, lambda run: run.state == RunState.PARKED)

    deadline = asyncio.get_event_loop().time() + 30
    while asyncio.get_event_loop().time() < deadline:
        rows = await _notifications_for_run(rt, workflow_id)
        (in_thread,) = [row for row in rows if row.conversation_id == conversation_id] or [None]
        if in_thread and in_thread.state == "delivered":
            break
        await asyncio.sleep(0.1)
    else:
        raise AssertionError(f"the thread was never notified; {deliver_spy.calls=}")
    # The chat asks, so the run's account gets no second notification.
    assert rows == [in_thread]
    assert in_thread.actions == [
        {"id": "approve", "label": "Approve"},
        {"id": "request_changes", "label": "Request changes"},
    ]
    assert in_thread.deep_link == f"https://druks.example/runs/{workflow_id}"
    (call,) = [call for call in deliver_spy.calls if call["token"] == in_thread.correlation_token]
    assert (call["channel"], call["thread_ts"]) == ("C9", "1700000000.000100")
    assert call["deep_link"] == in_thread.deep_link

    session = get_session(rt.engine)
    try:
        (approval,) = await session.scalars(
            select(Message).where(Message.conversation_id == conversation_id)
        )
        assert "Its buttons are in this thread" in approval.body
        assert (approval.is_internal, approval.state) == (True, MessageState.DEFERRED)
    finally:
        await session.close()
    # The ask with its buttons is the person's one message, so the park starts no turn.
    assert not await DBOS.list_workflows_async(name="chat.deliver", parent_workflow_id=workflow_id)

    # The review asks a question, so the click goes to the chat, and the run waits for
    # the agent's answer.
    clicked = await _respond_in_own_session(
        rt, in_thread.correlation_token, {"control": "approve"}, user_id="U1"
    )
    assert clicked == "ok"
    parked = await _wait_run(rt, workflow_id, lambda run: run.state == RunState.PARKED)
    session = get_session(rt.engine)
    try:
        bodies = await session.scalars(
            select(Message.body).where(
                Message.conversation_id == conversation_id,
                Message.role == MessageRole.USER,
                ~Message.is_internal,
            )
        )
        assert list(bodies) == ["Approve"]
    finally:
        await session.close()

    await parked.resume(action="approve", answers={"q1": "postgres"}, note="")
    await _wait_run(rt, workflow_id, lambda run: run.state == RunState.FINISHED)


async def test_concurrent_responds_resolve_to_one_answer(rt, deliver_spy):
    await _set_notification_channel(rt, "C-inbox-race")
    workflow_id = await rt.InAppFlow.start(subject=NotificationProbe(id=9010))
    await _wait_run(rt, workflow_id, lambda run: run.state == RunState.PARKED)
    notification = await _wait_notification(rt, workflow_id, "delivered")
    token = notification.correlation_token

    results = await asyncio.gather(
        asyncio.create_task(_respond_in_own_session(rt, token, {"control": "approve"})),
        asyncio.create_task(_respond_in_own_session(rt, token, {"control": "approve"})),
    )

    assert results.count("ok") == 1
    assert _dbos_replies(rt, workflow_id) == 1
    await _wait_run(rt, workflow_id, lambda run: run.state == RunState.FINISHED)
    assert (await _notifications_for_run(rt, workflow_id))[0].state == "acknowledged"


async def test_a_park_notifies_the_run_s_account_by_its_channel(rt, deliver_spy):
    async def configure():
        await _install_slack()
        account = await Account.get_or_create(db_session(), "explicit@example.com")
        await account.update_preferences(notification_channel_id="C-explicit")
        subject = NotificationProbe(id=9021)
        db_session.add(subject)
        return account.id, subject

    account_id, subject = await _seed(rt, configure)
    workflow_id = await rt.ExternalFlow.start(subject=subject, account_id=account_id)
    notification = await _wait_notification(rt, workflow_id, "delivered")
    assert notification.account_id == account_id
    (call,) = deliver_spy.calls
    assert call["channel"] == "C-explicit"


async def _create_question(user_id: str) -> Notification:
    """A question that a Slack chat's agent asked, as its turn left it."""
    await _install_slack()
    account = await Account.get_default(db_session())
    conversation = Conversation(
        account_id=account.id,
        source=ConversationSource.SLACK,
        user_id=user_id,
        thread_id="C9:1700000000.000300",
    )
    db_session().add(conversation)
    await db_session().flush()
    return await Notification.create(
        db_session(),
        conversation_id=conversation.id,
        reason="question.asked",
        body="I read both.\n\nWhich database?",
        question_offset=12,
        actions=[{"id": "1", "label": "Postgres"}, {"id": "2", "label": "MySQL"}],
    )


async def _ask(rt, user_id: str) -> Notification:
    """The question and its queued send, as the turn that asked commits them."""

    async def create():
        question = await _create_question(user_id)
        await outbox.queue_send(db_session(), question)
        return question

    return await _seed(rt, create)


async def _answer_in_words(rt, conversation_id: str) -> None:
    """What the person's typed answer does when their message reaches the agent."""

    async def close():
        await outbox.close_questions(db_session(), conversation_id)

    await _seed(rt, close)


async def _wait_workflow(rt, workflow_id: str, statuses=("SUCCESS", "ERROR")) -> str:
    deadline = asyncio.get_event_loop().time() + 30
    while asyncio.get_event_loop().time() < deadline:
        with rt.schema_engine.connect() as connection:
            status = connection.execute(
                text("SELECT status FROM dbos.workflow_status WHERE workflow_uuid = :id"),
                {"id": workflow_id},
            ).scalar_one_or_none()
        if status in statuses:
            return status
        await asyncio.sleep(0.1)
    raise AssertionError(f"workflow {workflow_id} is still {status}")


async def test_a_question_that_an_answer_closed_in_the_queue_posts_its_reply_closed(
    rt, deliver_spy
):
    async def create():
        question = await _create_question("U21")
        await outbox.close_questions(db_session(), question.conversation_id)
        await outbox.queue_send(db_session(), question)
        return question

    question = await _seed(rt, create)

    assert await _wait_workflow(rt, f"{CLOSE_QUESTION_WORKFLOW}:{question.id}") == "SUCCESS"
    done = await _snapshot(rt, question.id)
    assert (done["state"], done["message_id"]) == ("acknowledged", "C9:1700000000.000001")
    posted = deliver_spy.calls[0]
    assert (posted["message_ts"], posted["body"], posted["question_offset"]) == (
        "",
        "I read both.\n\nWhich database?",
        12,
    )
    assert (posted["actions"], posted["outcome"]) == (None, CLOSED_MESSAGE)


async def test_an_answer_while_a_question_posts_takes_its_buttons_off_after_the_post(
    rt, deliver_spy
):
    deliver_spy.hold = asyncio.Event()
    question = await _ask(rt, "U22")
    deadline = asyncio.get_event_loop().time() + 30
    while not deliver_spy.calls:
        assert asyncio.get_event_loop().time() < deadline, "the question never posted"
        await asyncio.sleep(0.1)

    await _answer_in_words(rt, question.conversation_id)
    close = f"{CLOSE_QUESTION_WORKFLOW}:{question.id}"
    await _wait_workflow(rt, close, statuses=("PENDING", "SUCCESS", "ERROR"))
    await asyncio.sleep(1.0)  # room for a close that does not wait for the post to end
    deliver_spy.hold.set()

    assert await _wait_workflow(rt, close) == "SUCCESS"
    done = await _snapshot(rt, question.id)
    posted, closed = deliver_spy.calls
    assert (done["state"], done["message_id"]) == ("acknowledged", "C9:1700000000.000001")
    assert (posted["actions"], posted["question_offset"]) == (question.actions, 12)
    assert (closed["message_ts"], closed["outcome"], closed["actions"]) == (
        "1700000000.000001",
        CLOSED_MESSAGE,
        None,
    )
    assert closed["question_offset"] == 12


async def test_a_refused_edit_leaves_the_question_closed_and_retries_nothing(rt, deliver_spy):
    question = await _ask(rt, "U24")
    await _wait_for(rt, question.id, lambda s: s["state"] == "delivered")
    deliver_spy.always_fail = True

    await _answer_in_words(rt, question.conversation_id)

    assert await _wait_workflow(rt, f"{CLOSE_QUESTION_WORKFLOW}:{question.id}") == "SUCCESS"
    _, refused = deliver_spy.calls
    assert refused["outcome"] == CLOSED_MESSAGE
    assert (await _snapshot(rt, question.id))["state"] == "acknowledged"

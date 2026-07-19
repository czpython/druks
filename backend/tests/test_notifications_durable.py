import asyncio
import os
from types import SimpleNamespace

import psycopg
import pytest
from conftest import configure_app_for_test, init_db, make_settings
from dbos import DBOS
from druks.database import configure_session, db_session, get_session
from druks.durable.engine import configure_engine, init_dbos, launch, shutdown
from druks.durable.enums import RunState
from druks.extensions.registry import workflows
from druks.notifications import outbox
from druks.notifications.exceptions import DeliveryError, NotificationError
from druks.notifications.models import Destination, Notification
from druks.notifications.outbox import notifications_queue, send_notification
from druks.notifications.services import respond_to_notification
from druks.user_settings.models import UserSettings
from druks.workflows import Gate, Run, Workflow
from fastapi.testclient import TestClient
from pydantic import BaseModel, Field
from sqlalchemy import create_engine, select, text

PG_BASE = os.environ.get("DRUKS_TEST_PG", "postgresql://druks:druks@localhost:5432")
DB = "druks_notifications_durable_test"
URL = f"{PG_BASE.replace('postgresql://', 'postgresql+psycopg://')}/{DB}"
_WEBHOOK_URL = "https://hooks.slack.com/services/T000/B000/durablesecret"


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


# What the in-app reviews resumed with — the respond round-trip asserts the
# payload arrived in the workflow verbatim.
_REVIEW_REPLIES: list[dict] = []


def _build_park_flows():
    class ParkNote(Gate):
        action: str = ""

        @classmethod
        async def on_wait(cls, workflow: Workflow) -> None:
            # Overridden so a subjectless park is allowed (the AC5 case).
            return

    class InAppFlow(Workflow):
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
        async def run_multistep(self) -> None:
            await ParkNote.wait(
                input_request={"presentation": "external", "label": "Answer on the ticket"}
            )

    class ExternalUrlFlow(Workflow):
        async def run_multistep(self) -> None:
            await ParkNote.wait(
                input_request={
                    "presentation": "external",
                    "label": "Review the PR",
                    "url": "https://github.com/acme/app/pull/7",
                }
            )

    class DoubleParkFlow(Workflow):
        async def run_multistep(self) -> None:
            await ParkNote.wait(input_request={"presentation": "external", "label": "Round one"})
            await ParkNote.wait(input_request={"presentation": "external", "label": "Round two"})

    return InAppFlow, ExternalFlow, ExternalUrlFlow, DoubleParkFlow


@pytest.fixture(scope="module", autouse=True)
def rt():
    db_url_snap = os.environ.get("DRUKS_DATABASE_URL")

    admin = psycopg.connect(f"{PG_BASE}/postgres", autocommit=True)
    admin.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
    admin.execute(f"CREATE DATABASE {DB}")
    admin.close()

    engine = create_engine(URL)
    init_db(engine)
    configure_engine(engine)
    configure_session(engine)
    in_app_flow, external_flow, external_url_flow, double_park_flow = _build_park_flows()
    os.environ["DRUKS_DATABASE_URL"] = URL
    # The outbox module was imported above — its queue + workflow register
    # before launch(), which is the wiring this whole module runs through.
    init_dbos()
    launch()
    try:
        yield SimpleNamespace(
            engine=engine,
            InAppFlow=in_app_flow,
            ExternalFlow=external_flow,
            ExternalUrlFlow=external_url_flow,
            DoubleParkFlow=double_park_flow,
        )
    finally:
        shutdown()
        engine.dispose()
        for kind in ("in_app_flow", "external_flow", "external_url_flow", "double_park_flow"):
            workflows._items.pop(kind, None)
        if db_url_snap is None:
            os.environ.pop("DRUKS_DATABASE_URL", None)
        else:
            os.environ["DRUKS_DATABASE_URL"] = db_url_snap


class _DeliverSpy:
    def __init__(self):
        self.calls: list[dict] = []
        self.failures_remaining = 0
        self.always_fail = False
        self.error: Exception = DeliveryError("spy", "HTTPStatusError")

    async def __call__(self, destination, body, *, actions=None, token=None, idempotency_key=None):
        self.calls.append(
            {
                "destination": destination.name,
                "body": body,
                "actions": actions,
                "token": token,
                "idempotency_key": idempotency_key,
            }
        )
        if self.always_fail:
            raise self.error
        if self.failures_remaining:
            self.failures_remaining -= 1
            raise self.error


@pytest.fixture
def deliver_spy(monkeypatch):
    spy = _DeliverSpy()
    monkeypatch.setattr(outbox, "deliver", spy)
    return spy


def _seed(rt, seeder):
    # Commit for real: the outbox worker reads through its own sessions.
    # Expunge first so the returned instance keeps its loaded attributes past
    # the commit's expiry.
    session = get_session(rt.engine)
    db_session.registry.set(session)
    try:
        result = seeder()
        session.flush()
        session.expunge_all()
        session.commit()
        return result
    finally:
        db_session.remove()
        session.close()


async def _deliver(rt, *, to, subject=None, reason="r", body="b", actions=None):
    # The create-seam path a producer uses (the gate-park producer's shape):
    # persist the row committed, then enqueue the outbox — no notify() hatch.
    notification_id = _seed(
        rt,
        lambda: (
            Notification.create(
                destination_id=Destination.get_for_name(to).id,
                reason=reason,
                body=body,
                subject=subject or {"type": "notification_probe", "id": 1},
                actions=actions,
            ).id
        ),
    )
    await notifications_queue.enqueue_async(send_notification, notification_id)
    return notification_id


def _snapshot(rt, notification_id) -> dict:
    session = get_session(rt.engine)
    try:
        notification = session.get(Notification, notification_id)
        return {
            "state": notification.state,
            "attempts": notification.attempts,
            "last_error": notification.last_error,
            "delivered_at": notification.delivered_at,
            "token": notification.correlation_token,
        }
    finally:
        session.close()


async def _wait_for(rt, notification_id, predicate, timeout=30.0):
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        snapshot = _snapshot(rt, notification_id)
        if predicate(snapshot):
            return snapshot
        await asyncio.sleep(0.1)
    raise AssertionError(f"timed out; last={_snapshot(rt, notification_id)}")


async def test_outbox_delivers_with_actions_token_and_key(rt, deliver_spy):
    _seed(rt, lambda: Destination.create(name="happy", kind="slack_webhook", url=_WEBHOOK_URL))

    notification_id = await _deliver(
        rt, to="happy", reason="ops.alert", body="hello", actions=[{"id": "ok", "label": "OK"}]
    )

    done = await _wait_for(rt, notification_id, lambda s: s["state"] == "delivered")
    assert done["attempts"] == 1
    assert done["delivered_at"] is not None
    (call,) = deliver_spy.calls
    assert call["destination"] == "happy"
    assert call["body"] == "hello"
    assert call["actions"] == [{"id": "ok", "label": "OK"}]
    assert call["token"] == done["token"]
    assert call["idempotency_key"] == notification_id


async def test_transient_failure_retries_to_delivered_and_touches_no_run(rt, deliver_spy):
    deliver_spy.failures_remaining = 2
    _seed(rt, lambda: Destination.create(name="flaky", kind="slack_webhook", url=_WEBHOOK_URL))

    notification_id = await _deliver(rt, to="flaky")

    done = await _wait_for(rt, notification_id, lambda s: s["state"] == "delivered")
    assert done["attempts"] == 3
    assert len(deliver_spy.calls) == 3
    # Delivery is decoupled from the run lifecycle: no Run row exists or was touched.
    with rt.engine.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM durable_runs")).scalar_one() == 0


async def test_terminal_failure_marks_failed_sanitized_and_reads_back(
    rt, deliver_spy, monkeypatch, tmp_path
):
    deliver_spy.always_fail = True
    deliver_spy.error = DeliveryError("doomed", "HTTPStatusError")
    # Two attempts instead of five: same terminal path, fraction of the backoff.
    monkeypatch.setattr(outbox, "_SEND_RETRIES", {"retries_allowed": True, "max_attempts": 2})
    _seed(rt, lambda: Destination.create(name="doomed", kind="slack_webhook", url=_WEBHOOK_URL))

    notification_id = await _deliver(rt, to="doomed")

    done = await _wait_for(rt, notification_id, lambda s: s["state"] == "failed")
    assert done["attempts"] == 2
    assert len(deliver_spy.calls) == 2
    assert "doomed" in done["last_error"]
    assert _WEBHOOK_URL not in done["last_error"]

    client = TestClient(configure_app_for_test(settings=make_settings(tmp_path), engine=rt.engine))
    shown = client.get(f"/api/notifications/{notification_id}")
    assert shown.status_code == 200
    assert shown.json()["state"] == "failed"
    assert shown.json()["attempts"] == 2
    assert _WEBHOOK_URL not in shown.text
    assert done["token"] not in shown.text


async def test_unexpected_error_reduces_to_class_name(rt, deliver_spy, monkeypatch):
    # A misbehaving transport that leaks the URL in its message must never
    # reach last_error verbatim.
    deliver_spy.always_fail = True
    deliver_spy.error = RuntimeError(f"boom at {_WEBHOOK_URL}")
    monkeypatch.setattr(outbox, "_SEND_RETRIES", {"retries_allowed": True, "max_attempts": 2})
    _seed(rt, lambda: Destination.create(name="leaky", kind="slack_webhook", url=_WEBHOOK_URL))

    notification_id = await _deliver(rt, to="leaky")

    done = await _wait_for(rt, notification_id, lambda s: s["state"] == "failed")
    assert done["last_error"] == "RuntimeError"
    assert _WEBHOOK_URL not in done["last_error"]


async def test_rerun_on_delivered_notification_skips_the_send(rt, deliver_spy):
    _seed(rt, lambda: Destination.create(name="once", kind="slack_webhook", url=_WEBHOOK_URL))
    notification_id = await _deliver(rt, to="once")
    await _wait_for(rt, notification_id, lambda s: s["state"] == "delivered")
    assert len(deliver_spy.calls) == 1

    handle = await notifications_queue.enqueue_async(send_notification, notification_id)
    await handle.get_result()

    assert len(deliver_spy.calls) == 1


async def test_create_seam_plus_direct_enqueue_delivers(rt, deliver_spy):
    destination = _seed(
        rt, lambda: Destination.create(name="seam", kind="slack_webhook", url=_WEBHOOK_URL)
    )
    notification = _seed(
        rt,
        lambda: Notification.create(
            destination_id=destination.id, reason="r", body="b", subject={"type": "probe", "id": 1}
        ),
    )
    # The create seam persisted a pending row and enqueued nothing.
    assert _snapshot(rt, notification.id)["state"] == "pending"
    assert deliver_spy.calls == []

    handle = await notifications_queue.enqueue_async(send_notification, notification.id)
    await handle.get_result()

    assert _snapshot(rt, notification.id)["state"] == "delivered"
    assert len(deliver_spy.calls) == 1


# --- gate-park notifications ---------------------------------------------------


def _set_gate_park_pointer(rt, destination_id):
    session = get_session(rt.engine)
    db_session.registry.set(session)
    try:
        UserSettings.get().set_gate_park_destination(destination_id)
        session.commit()
    finally:
        db_session.remove()
        session.close()


def _run_snapshot(rt, workflow_id) -> Run:
    session = get_session(rt.engine)
    try:
        return session.get(Run, workflow_id)
    finally:
        session.close()


def _notifications_for_run(rt, workflow_id) -> list[Notification]:
    session = get_session(rt.engine)
    try:
        return list(
            session.execute(
                select(Notification)
                .where(Notification.run_id == workflow_id)
                .order_by(Notification.id)
            )
            .scalars()
            .all()
        )
    finally:
        session.close()


async def _wait_run(rt, workflow_id, predicate, timeout=30.0) -> Run:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        run = _run_snapshot(rt, workflow_id)
        if run and predicate(run):
            return run

        await asyncio.sleep(0.1)
    raise AssertionError(f"timed out; last={_run_snapshot(rt, workflow_id)}")


async def _wait_notification(rt, workflow_id, state, timeout=30.0) -> Notification:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        rows = _notifications_for_run(rt, workflow_id)
        if rows and rows[0].state == state:
            return rows[0]
        await asyncio.sleep(0.1)
    raise AssertionError(f"timed out; last={_notifications_for_run(rt, workflow_id)}")


async def test_in_app_park_notifies_with_actions(rt, deliver_spy):
    destination = _seed(
        rt, lambda: Destination.create(name="inbox-inapp", kind="slack_webhook", url=_WEBHOOK_URL)
    )
    _set_gate_park_pointer(rt, destination.id)

    workflow_id = (
        await rt.InAppFlow.start(subject={"type": "notification_probe", "id": 9001})
    ).run_id
    parked = await _wait_run(rt, workflow_id, lambda run: run.state == RunState.PENDING_INPUT)

    notification = await _wait_notification(rt, workflow_id, "delivered")
    assert len(_notifications_for_run(rt, workflow_id)) == 1
    assert notification.reason == "gate.parked"
    assert notification.body.startswith("Review")
    assert "Which database?" in notification.body
    assert notification.actions == [
        {"id": "approve", "label": "Approve"},
        {"id": "request_changes", "label": "Request changes"},
        {"id": "cancel", "label": "Cancel"},
        {"id": "postgres", "label": "Postgres"},
    ]
    assert notification.deep_link is None
    assert notification.subject == {"type": "notification_probe", "id": 9001}
    assert notification.run_id == workflow_id
    assert notification.run_parked_at == parked.input_requested_at

    (call,) = deliver_spy.calls
    assert call["actions"] == notification.actions
    assert call["token"] == notification.correlation_token


async def test_external_park_notifies_without_actions(rt, deliver_spy):
    destination = _seed(
        rt, lambda: Destination.create(name="inbox-ext", kind="slack_webhook", url=_WEBHOOK_URL)
    )
    _set_gate_park_pointer(rt, destination.id)

    workflow_id = (
        await rt.ExternalFlow.start(subject={"type": "notification_probe", "id": 9002})
    ).run_id
    await _wait_run(rt, workflow_id, lambda run: run.state == RunState.PENDING_INPUT)

    notification = await _wait_notification(rt, workflow_id, "delivered")
    assert notification.body == "Answer on the ticket"
    assert notification.actions is None
    assert notification.deep_link is None
    (call,) = deliver_spy.calls
    assert call["actions"] is None


async def test_external_park_with_declared_url_sets_deep_link(rt, deliver_spy):
    destination = _seed(
        rt, lambda: Destination.create(name="inbox-url", kind="slack_webhook", url=_WEBHOOK_URL)
    )
    _set_gate_park_pointer(rt, destination.id)

    workflow_id = (
        await rt.ExternalUrlFlow.start(subject={"type": "notification_probe", "id": 9003})
    ).run_id
    await _wait_run(rt, workflow_id, lambda run: run.state == RunState.PENDING_INPUT)

    notification = await _wait_notification(rt, workflow_id, "delivered")
    assert notification.deep_link == "https://github.com/acme/app/pull/7"
    assert notification.actions is None


async def test_no_designated_destination_notifies_nothing(rt, deliver_spy):
    _set_gate_park_pointer(rt, None)

    in_app_id = (
        await rt.InAppFlow.start(subject={"type": "notification_probe", "id": 9004})
    ).run_id
    external_id = (
        await rt.ExternalFlow.start(subject={"type": "notification_probe", "id": 9014})
    ).run_id
    await _wait_run(rt, in_app_id, lambda run: run.state == RunState.PENDING_INPUT)
    await _wait_run(rt, external_id, lambda run: run.state == RunState.PENDING_INPUT)

    await asyncio.sleep(1.0)
    assert _notifications_for_run(rt, in_app_id) == []
    assert _notifications_for_run(rt, external_id) == []
    assert deliver_spy.calls == []


async def test_deleted_designated_destination_notifies_nothing(rt, deliver_spy):
    destination = _seed(
        rt,
        lambda: Destination.create(name="inbox-deleted", kind="slack_webhook", url=_WEBHOOK_URL),
    )
    _set_gate_park_pointer(rt, destination.id)
    _seed(rt, lambda: Destination.get(destination.id).delete())

    workflow_id = (
        await rt.ExternalFlow.start(subject={"type": "notification_probe", "id": 9005})
    ).run_id
    await _wait_run(rt, workflow_id, lambda run: run.state == RunState.PENDING_INPUT)

    await asyncio.sleep(1.0)
    assert _notifications_for_run(rt, workflow_id) == []
    assert deliver_spy.calls == []
    # ON DELETE SET NULL cleared the pointer itself.
    session = get_session(rt.engine)
    try:
        assert session.get(UserSettings, UserSettings.SINGLETON_ID).gate_park_destination_id is None
    finally:
        session.close()


async def test_subjectless_park_notifies_nothing(rt, deliver_spy):
    destination = _seed(
        rt, lambda: Destination.create(name="inbox-nosubj", kind="slack_webhook", url=_WEBHOOK_URL)
    )
    _set_gate_park_pointer(rt, destination.id)

    workflow_id = (await rt.ExternalFlow.start(subject=None)).run_id
    await _wait_run(rt, workflow_id, lambda run: run.state == RunState.PENDING_INPUT)

    await asyncio.sleep(1.0)
    assert _notifications_for_run(rt, workflow_id) == []
    assert deliver_spy.calls == []


async def test_failed_delivery_leaves_run_parked_and_resumable(rt, deliver_spy, monkeypatch):
    deliver_spy.always_fail = True
    monkeypatch.setattr(outbox, "_SEND_RETRIES", {"retries_allowed": True, "max_attempts": 2})
    destination = _seed(
        rt, lambda: Destination.create(name="inbox-flaky", kind="slack_webhook", url=_WEBHOOK_URL)
    )
    _set_gate_park_pointer(rt, destination.id)

    workflow_id = (
        await rt.InAppFlow.start(subject={"type": "notification_probe", "id": 9006})
    ).run_id
    await _wait_run(rt, workflow_id, lambda run: run.state == RunState.PENDING_INPUT)
    notification = await _wait_notification(rt, workflow_id, "failed")
    assert _WEBHOOK_URL not in notification.last_error

    # The park never noticed the dead endpoint: still waiting, still resumable.
    parked = _run_snapshot(rt, workflow_id)
    assert parked.state == RunState.PENDING_INPUT
    await parked.resume(action="approve")
    await _wait_run(rt, workflow_id, lambda run: run.state == RunState.FINISHED)


async def test_replayed_park_notifies_once(rt, deliver_spy):
    destination = _seed(
        rt, lambda: Destination.create(name="inbox-replay", kind="slack_webhook", url=_WEBHOOK_URL)
    )
    _set_gate_park_pointer(rt, destination.id)

    workflow_id = (
        await rt.ExternalFlow.start(subject={"type": "notification_probe", "id": 9007})
    ).run_id
    await _wait_run(rt, workflow_id, lambda run: run.state == RunState.PENDING_INPUT)
    await _wait_notification(rt, workflow_id, "delivered")
    assert len(deliver_spy.calls) == 1

    def _execution_claim():
        with rt.engine.connect() as connection:
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
    await _wait_run(rt, workflow_id, lambda run: run.state == RunState.PENDING_INPUT)
    await asyncio.sleep(1.0)  # room for a wrong duplicate enqueue to land

    assert len(_notifications_for_run(rt, workflow_id)) == 1
    assert len(deliver_spy.calls) == 1

    # Two recv waiters now share the topic (the pre-resume one and the
    # replayed one); cancel ends the run and both.
    session = get_session(rt.engine)
    db_session.registry.set(session)
    try:
        await session.get(Run, workflow_id).cancel()
        session.commit()
    finally:
        db_session.remove()
        session.close()


async def test_each_park_round_gets_its_own_notification(rt, deliver_spy):
    destination = _seed(
        rt, lambda: Destination.create(name="inbox-rounds", kind="slack_webhook", url=_WEBHOOK_URL)
    )
    _set_gate_park_pointer(rt, destination.id)

    workflow_id = (
        await rt.DoubleParkFlow.start(subject={"type": "notification_probe", "id": 9008})
    ).run_id
    first_round = await _wait_run(rt, workflow_id, lambda run: run.state == RunState.PENDING_INPUT)
    await _wait_notification(rt, workflow_id, "delivered")
    assert len(deliver_spy.calls) == 1

    await first_round.resume(action="go")
    # The same code path parks again — a later step invocation, so a new
    # checkpoint, a new row, a new delivery.
    await _wait_run(
        rt,
        workflow_id,
        lambda run: (
            run.state == RunState.PENDING_INPUT
            and run.input_requested_at != first_round.input_requested_at
        ),
    )
    deadline = asyncio.get_event_loop().time() + 30
    while asyncio.get_event_loop().time() < deadline:
        rows = _notifications_for_run(rt, workflow_id)
        if len(rows) == 2 and rows[1].state == "delivered":
            break
        await asyncio.sleep(0.1)
    else:
        raise AssertionError(f"second round never notified; {deliver_spy.calls=}")

    rows = _notifications_for_run(rt, workflow_id)
    assert [row.body for row in rows] == ["Round one", "Round two"]
    assert rows[0].run_parked_at != rows[1].run_parked_at
    assert len(deliver_spy.calls) == 2


# --- respond: the inbound half against a live parked run ---------------------


async def _respond_in_own_session(rt, token, choice):
    # Each caller is its own task, so the scoped registry gives each its own
    # session — a real request-shaped transaction. (The HTTP layer itself is
    # pinned by the unit route tests; driving DBOS's send through a TestClient
    # portal loop here would poison the module's shared executor.)
    session = get_session(rt.engine)
    db_session.registry.set(session)
    try:
        await respond_to_notification(token, choice)
        session.commit()
        return "ok"
    except NotificationError as error:
        session.rollback()
        return type(error).__name__
    finally:
        db_session.remove()
        session.close()


def _dbos_replies(rt, workflow_id) -> int:
    with rt.engine.connect() as connection:
        return connection.execute(
            text(
                "SELECT count(*) FROM dbos.notifications"
                " WHERE destination_uuid = :id AND topic = 'review'"
            ),
            {"id": workflow_id},
        ).scalar_one()


async def test_respond_round_trip_finishes_the_run(rt, deliver_spy):
    destination = _seed(
        rt, lambda: Destination.create(name="inbox-respond", kind="slack_webhook", url=_WEBHOOK_URL)
    )
    _set_gate_park_pointer(rt, destination.id)
    workflow_id = (
        await rt.InAppFlow.start(subject={"type": "notification_probe", "id": 9009})
    ).run_id
    await _wait_run(rt, workflow_id, lambda run: run.state == RunState.PENDING_INPUT)
    notification = await _wait_notification(rt, workflow_id, "delivered")
    token = notification.correlation_token

    first = await _respond_in_own_session(
        rt, token, {"control": "approve", "answers": {"q1": "postgres"}, "note": ""}
    )
    assert first == "ok"

    await _wait_run(rt, workflow_id, lambda run: run.state == RunState.FINISHED)
    assert {"action": "approve", "answers": {"q1": "postgres"}, "note": ""} in _REVIEW_REPLIES
    assert _notifications_for_run(rt, workflow_id)[0].state == "acknowledged"

    # Sequential second answer: the acknowledged fast-path rejects it, and the
    # round's DBOS bookkeeping still holds exactly one reply.
    second = await _respond_in_own_session(rt, token, {"control": "approve"})
    assert second == "AlreadyAcknowledgedError"
    assert _dbos_replies(rt, workflow_id) == 1


async def test_concurrent_responds_resolve_to_one_answer(rt, deliver_spy):
    destination = _seed(
        rt, lambda: Destination.create(name="inbox-race", kind="slack_webhook", url=_WEBHOOK_URL)
    )
    _set_gate_park_pointer(rt, destination.id)
    workflow_id = (
        await rt.InAppFlow.start(subject={"type": "notification_probe", "id": 9010})
    ).run_id
    await _wait_run(rt, workflow_id, lambda run: run.state == RunState.PENDING_INPUT)
    notification = await _wait_notification(rt, workflow_id, "delivered")
    token = notification.correlation_token

    results = await asyncio.gather(
        asyncio.create_task(_respond_in_own_session(rt, token, {"control": "approve"})),
        asyncio.create_task(_respond_in_own_session(rt, token, {"control": "approve"})),
    )

    assert results.count("ok") == 1
    assert _dbos_replies(rt, workflow_id) == 1
    await _wait_run(rt, workflow_id, lambda run: run.state == RunState.FINISHED)
    assert _notifications_for_run(rt, workflow_id)[0].state == "acknowledged"

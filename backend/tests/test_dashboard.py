from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, Mock
from zoneinfo import ZoneInfo

import pytest
from dbos import DBOS
from druks.accounts.models import Account
from druks.api import dashboard
from druks.durable.dbos_state import workflow_status
from druks.durable.models import Artifact, Run
from druks.events.models import Event
from druks.testing import configure_app_for_test, make_settings, seed_call, seed_run
from druks.user_settings.models import SettingsOverride
from druks_field_notes.models import Note
from druks_field_notes.workflows import Summarize
from fastapi.testclient import TestClient
from uuid_utils import uuid7


@pytest.fixture
def client(tmp_path, druks_db):
    app = configure_app_for_test(settings=make_settings(tmp_path))
    with TestClient(app) as client:
        yield client


def overview(client, app=None):
    response = client.get("/api/dashboard/overview", params={"app": app} if app else {})
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    return response.json()


async def test_schedules_resolve_paused_override_and_operator_timezone(
    client, druks_db, monkeypatch
):
    monkeypatch.setattr(Summarize, "every", "0 9 * * *")
    await SettingsOverride.set_workflow_setting(druks_db, Summarize.kind, "schedule", "15 10 * * 1")
    await SettingsOverride.set_workflow_setting(druks_db, Summarize.kind, "schedule_enabled", False)
    settings = dashboard.load_settings().model_copy(update={"timezone": "Europe/Madrid"})
    monkeypatch.setattr(dashboard, "load_settings", lambda: settings)

    response = client.get("/api/dashboard/schedules")

    assert response.status_code == 200
    assert {
        "app": "field_notes",
        "kind": Summarize.kind,
        "cron": "15 10 * * 1",
        "defaultCron": "0 9 * * *",
        "enabled": False,
        "timezone": "Europe/Madrid",
        "nextRunAt": None,
        "runs": [],
    } in response.json()["rows"]


def test_dashboard_requires_the_existing_identity_gate(tmp_path, druks_db):
    app = configure_app_for_test(
        settings=make_settings(tmp_path, identity={"mode": "header", "header": "X-Edge-Email"}),
        authenticated=False,
    )
    with TestClient(app) as anonymous:
        assert anonymous.get("/api/dashboard/overview").status_code == 401
        assert anonymous.get("/api/dashboard/schedules").status_code == 401
        assert (
            anonymous.post("/api/dashboard/schedules/field_notes.summarize/run").status_code == 401
        )


async def test_schedule_history_is_bounded_and_excludes_downstream_runs(
    client, druks_db, monkeypatch
):
    monkeypatch.setattr(Summarize, "every", "0 9 * * *")
    timestamp = int(datetime(2026, 1, 1, tzinfo=UTC).timestamp() * 1000)
    for index in range(12):
        await druks_db.execute(
            workflow_status.insert().values(
                workflow_uuid=f"invocation-{index}",
                schedule_name=Summarize.kind,
                status="ERROR" if index == 11 else "SUCCESS",
                created_at=timestamp + index * 60_000,
                started_at_epoch_ms=timestamp + index * 60_000 + 1_000,
                completed_at=timestamp + index * 60_000 + 39_000,
            )
        )
    await druks_db.execute(
        workflow_status.insert().values(
            workflow_uuid="downstream", status="PENDING", created_at=timestamp
        )
    )

    response = client.get("/api/dashboard/schedules")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    row = next(row for row in response.json()["rows"] if row["kind"] == Summarize.kind)
    assert [run["run"] for run in row["runs"]] == [
        f"invocation-{index}" for index in range(11, 3, -1)
    ]
    assert row["runs"][0] == {
        "run": "invocation-11",
        "status": "ERROR",
        "createdAt": "2026-01-01T00:11:00Z",
        "startedAt": "2026-01-01T00:11:01Z",
        "finishedAt": "2026-01-01T00:11:39Z",
    }


async def test_next_invocation_uses_installation_wall_clock(client, monkeypatch):
    monkeypatch.setattr(Summarize, "every", "0 9 * * *")
    settings = dashboard.load_settings().model_copy(update={"timezone": "Europe/Madrid"})
    monkeypatch.setattr(dashboard, "load_settings", lambda: settings)
    now = datetime.now(UTC)

    response = client.get("/api/dashboard/schedules")

    row = next(row for row in response.json()["rows"] if row["kind"] == Summarize.kind)
    next_run = datetime.fromisoformat(row["nextRunAt"]).astimezone(ZoneInfo("Europe/Madrid"))
    assert next_run.hour == 9
    assert next_run.minute == 0
    assert now < next_run < now + timedelta(hours=26)


@pytest.mark.parametrize("cron", ["0 0 31 2 *", "*/10 * * * * *"])
async def test_schedule_with_no_future_date_or_seconds(client, monkeypatch, cron):
    monkeypatch.setattr(Summarize, "every", cron)

    response = client.get("/api/dashboard/schedules")

    assert response.status_code == 200
    row = next(row for row in response.json()["rows"] if row["kind"] == Summarize.kind)
    if cron == "0 0 31 2 *":
        assert row["nextRunAt"] is None
    else:
        assert datetime.fromisoformat(row["nextRunAt"]).second % 10 == 0


async def test_run_now_triggers_a_paused_schedule_without_changing_overrides(
    client, druks_db, monkeypatch
):
    monkeypatch.setattr(Summarize, "every", "0 9 * * *")
    await SettingsOverride.set_workflow_setting(druks_db, Summarize.kind, "schedule_enabled", False)
    monkeypatch.setattr(DBOS, "get_schedule_async", AsyncMock(return_value={"status": "PAUSED"}))
    trigger = Mock(return_value=Mock(get_workflow_id=Mock(return_value="scheduled-invocation")))
    monkeypatch.setattr(DBOS, "trigger_schedule", trigger)

    response = client.post(f"/api/dashboard/schedules/{Summarize.kind}/run")

    assert response.status_code == 202
    assert response.json() == {"run": "scheduled-invocation"}
    trigger.assert_called_once_with(Summarize.kind)
    assert not await Summarize.has_enabled_schedule(druks_db)


async def test_run_now_rejects_unknown_unscheduled_and_unavailable_workflows(client, monkeypatch):
    assert client.post("/api/dashboard/schedules/missing/run").status_code == 404
    monkeypatch.setattr(Summarize, "every", None)
    assert client.post(f"/api/dashboard/schedules/{Summarize.kind}/run").status_code == 404
    monkeypatch.setattr(Summarize, "every", "0 9 * * *")
    monkeypatch.setattr(DBOS, "get_schedule_async", AsyncMock(return_value=None))
    assert client.post(f"/api/dashboard/schedules/{Summarize.kind}/run").status_code == 503


@pytest.mark.parametrize("has_artifact", [True, False])
async def test_artifact_title_comes_from_the_latest_call(client, druks_db, has_artifact):
    note = await Note.create(body="Confirm the delivery date")
    run = await seed_run(
        druks_db,
        kind=Summarize.kind,
        subject=note,
        state="parked",
        input_gate="review",
        input_request={"presentation": "in_app", "controls": ["approve"]},
    )
    run.input_requested_at = datetime(2026, 1, 2, tzinfo=UTC)
    first_call = await seed_call(druks_db, run=run, agent="summarize")
    druks_db.add(
        Artifact(
            agent_call_id=first_call.id, kind="markdown", title="Older proposal", path="old.md"
        )
    )
    first_call.created_at = datetime(2026, 1, 1, tzinfo=UTC)
    latest_call = await seed_call(druks_db, run=run, agent="summarize")
    if has_artifact:
        druks_db.add(
            Artifact(
                agent_call_id=latest_call.id,
                kind="markdown",
                title="Reply with the confirmed date",
                path="artifact.md",
            )
        )
    await druks_db.flush()

    response = client.get("/api/dashboard/overview")
    assert response.status_code == 200
    [row] = response.json()["needsYou"]["rows"]
    assert row["requestLabel"] is None
    assert row["artifactTitle"] == ("Reply with the confirmed date" if has_artifact else None)


async def test_exact_totals_keep_old_requests_beyond_two_hundred_runs(client, druks_db):
    request = await seed_run(
        druks_db,
        kind=Summarize.kind,
        state="parked",
        input_gate="review",
        input_request={"presentation": "external", "label": "Confirm delivery"},
    )
    request.input_requested_at = datetime(2026, 1, 1, tzinfo=UTC)
    for _ in range(205):
        await seed_run(druks_db, kind=Summarize.kind)

    body = overview(client)

    assert body["needsYou"]["total"] == 1
    assert body["needsYou"]["rows"][0]["run"] == request.id
    assert body["running"]["total"] == 205
    assert len(body["running"]["rows"]) == 4
    assert body["failed"] == {"total": 0, "rows": []}


@pytest.mark.parametrize(
    "state,section", [("parked", "needsYou"), ("running", "running"), ("failed", "failed")]
)
async def test_previews_have_stable_time_and_id_order(client, druks_db, state, section):
    timestamp = datetime(2026, 1, 1, tzinfo=UTC)
    runs = []
    for index in range(6):
        run = await seed_run(
            druks_db,
            kind=Summarize.kind,
            state=state,
            input_gate="review" if state == "parked" else None,
            input_request={"presentation": "in_app"} if state == "parked" else None,
        )
        run.created_at = timestamp
        if state == "parked":
            run.input_requested_at = timestamp + timedelta(hours=index // 2)
        await druks_db.execute(
            workflow_status.update()
            .where(workflow_status.c.workflow_uuid == run.id)
            .values(updated_at=int((timestamp + timedelta(hours=index // 2)).timestamp() * 1000))
        )
        runs.append(run.id)
    await druks_db.flush()

    result = overview(client)[section]

    assert result["total"] == 6
    expected = runs if state == "parked" else list(reversed(runs))
    assert [row["run"] for row in result["rows"]] == expected[:4]


async def test_newest_success_replaces_failure_and_non_requests_are_absent(client, druks_db):
    note = await Note.create(body="Recovered")
    failed = await seed_run(druks_db, kind=Summarize.kind, subject=note, state="failed")
    failed.created_at = datetime(2026, 1, 1, tzinfo=UTC)
    await seed_run(druks_db, kind=Summarize.kind, subject=note, state="finished")
    await seed_run(druks_db, kind=Summarize.kind, state="scheduled")
    for request, requested_at in [
        ({"presentation": "in_app"}, None),
        ({"label": "Internal wait"}, datetime.now(UTC)),
        ({"presentation": ""}, datetime.now(UTC)),
    ]:
        run = await seed_run(
            druks_db, kind=Summarize.kind, state="parked", input_gate="wait", input_request=request
        )
        run.input_requested_at = requested_at
    await druks_db.flush()

    assert overview(client) == {
        "needsYou": {"total": 0, "rows": []},
        "running": {"total": 0, "rows": []},
        "failed": {"total": 0, "rows": []},
        "lastFinishedAt": None,
        "lastFailedAt": None,
    }


async def test_subjectless_orphans_and_other_accounts_stay_visible(client, druks_db):
    other = await Account.get_or_create(druks_db, "another@example.invalid")
    first = await seed_run(druks_db, kind=Summarize.kind, account_id=other.id)
    second = await seed_run(druks_db, kind=Summarize.kind, account_id=other.id)
    orphan = Run(
        account_id=other.id,
        id=str(uuid7()),
        kind=Summarize.kind,
        created_at=datetime.now(UTC) - timedelta(minutes=10),
    )
    druks_db.add(orphan)
    await druks_db.flush()

    body = overview(client)

    assert body["running"]["total"] == 2
    assert {row["run"] for row in body["running"]["rows"]} == {first.id, second.id}
    assert body["failed"]["total"] == 1
    assert body["failed"]["rows"][0]["run"] == orphan.id
    assert body["failed"]["rows"][0]["state"] == "orphaned"
    assert body["failed"]["rows"][0]["subjectId"] is None


async def test_app_filter_scopes_counts_previews_and_recorded_times(client, druks_db):
    await seed_run(druks_db, kind=Summarize.kind)
    await seed_run(druks_db, kind="uninstalled.sweep")
    timestamp = datetime(2026, 1, 1, tzinfo=UTC)
    for app, event_type, offset in [
        ("field_notes", "workflow.finished", 0),
        ("field_notes", "workflow.failed", 1),
        ("field_notes", "agent.failed", 9),
        ("software_factory", "workflow.finished", 2),
        ("software_factory", "workflow.failed", 3),
        ("uninstalled", "workflow.finished", 10),
        ("uninstalled", "workflow.failed", 10),
        (None, "workflow.failed", 10),
    ]:
        druks_db.add(Event(app=app, type=event_type, created_at=timestamp + timedelta(days=offset)))
    await druks_db.flush()

    body = overview(client)
    assert body["running"]["total"] == 1
    assert body["lastFinishedAt"] == "2026-01-03T00:00:00Z"
    assert body["lastFailedAt"] == "2026-01-04T00:00:00Z"
    filtered = overview(client, "field_notes")
    assert filtered["running"]["total"] == 1
    assert filtered["running"]["rows"][0]["app"] == "field_notes"
    assert filtered["lastFinishedAt"] == "2026-01-01T00:00:00Z"
    assert filtered["lastFailedAt"] == "2026-01-02T00:00:00Z"
    empty = overview(client, "software_factory")
    assert empty["running"] == {"total": 0, "rows": []}
    assert empty["lastFinishedAt"] == "2026-01-03T00:00:00Z"
    assert client.get("/api/dashboard/overview?app=uninstalled").status_code == 404
    assert client.get("/api/dashboard/overview?app=").status_code == 404


async def test_preview_bounds_text_and_preserves_request_identity(client, druks_db):
    note = await Note.create(body="s" * 300)
    run = await seed_run(
        druks_db,
        kind=Summarize.kind,
        subject=note,
        state="parked",
        input_gate="review",
        input_request={"presentation": "in_app", "label": "r" * 300, "questions": ["private"]},
        failure="f" * 4000,
    )
    run.input_requested_at = datetime(2026, 1, 1, tzinfo=UTC)
    call = await seed_call(druks_db, run=run, agent="summarize")
    druks_db.add(
        Artifact(agent_call_id=call.id, kind="markdown", title="a" * 300, path="private.md")
    )
    await druks_db.flush()

    [row] = overview(client)["needsYou"]["rows"]

    assert row["run"] == run.id
    assert row["parkedAt"] == "2026-01-01T00:00:00Z"
    assert row["requestLabel"] == "r" * 240
    assert len(row["subjectKey"]) <= 240
    assert row["artifactTitle"] == "a" * 240
    assert row["failure"] == "f" * 2048
    assert "private" not in str(row)
    assert "transcript" not in str(row)

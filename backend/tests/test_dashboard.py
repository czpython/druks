from datetime import UTC, datetime, timedelta

import pytest
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
    await SettingsOverride.set_workflow_setting(Summarize.kind, "schedule", "15 10 * * 1")
    await SettingsOverride.set_workflow_setting(Summarize.kind, "schedule_enabled", False)
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
    } in response.json()["rows"]


def test_dashboard_requires_the_existing_identity_gate(tmp_path, druks_db):
    app = configure_app_for_test(
        settings=make_settings(tmp_path, identity={"mode": "header", "header": "X-Edge-Email"}),
        authenticated=False,
    )
    with TestClient(app) as anonymous:
        assert anonymous.get("/api/dashboard/overview").status_code == 401
        assert anonymous.get("/api/dashboard/schedules").status_code == 401


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
    other = await Account.get_or_create("another@example.invalid")
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
    assert len(row["subjectLabel"]) <= 240
    assert row["artifactTitle"] == "a" * 240
    assert row["failure"] == "f" * 2048
    assert "private" not in str(row)
    assert "transcript" not in str(row)

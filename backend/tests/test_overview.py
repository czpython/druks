from datetime import UTC, datetime, timedelta

import pytest
from druks.accounts.models import Account
from druks.api import overview
from druks.durable.dbos_state import workflow_status
from druks.durable.models import Run
from druks.testing import configure_app_for_test, make_settings, seed_run
from druks.user_settings.models import SettingsOverride, UserSettings
from druks_field_notes.models import Note
from druks_field_notes.workflows import Summarize
from fastapi.testclient import TestClient
from uuid_utils import uuid7


@pytest.fixture
def client(tmp_path, druks_db):
    app = configure_app_for_test(settings=make_settings(tmp_path))
    with TestClient(app) as client:
        yield client


def current_work(client):
    response = client.get("/api/overview/work")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    return response.json()


async def test_a_current_run_carries_request_facts_with_bounded_prose(client, druks_db):
    note = await Note.create(body="decision")
    run = await seed_run(
        druks_db,
        kind=Summarize.kind,
        subject=note,
        state="parked",
        input_gate="review",
        input_request={"presentation": "in_app", "label": "x" * 300, "questions": ["private"]},
        failure="f" * 700,
    )
    run.input_requested_at = datetime(2026, 1, 1, tzinfo=UTC)
    await druks_db.flush()

    body = current_work(client)

    assert body["hasMore"] is False
    [row] = body["rows"]
    assert (row["run"], row["app"], row["state"]) == (run.id, "field_notes", "parked")
    assert row["parkedAt"] == "2026-01-01T00:00:00Z"
    assert row["presentation"] == "in_app"
    assert row["requestLabel"] == "x" * 240
    assert row["failure"] == "f" * 512
    assert "private" not in str(row)


async def test_newer_success_hides_historical_failure(client, druks_db):
    note = await Note.create(body="recovered")
    failed = await seed_run(druks_db, kind=Summarize.kind, subject=note, state="failed")
    failed.created_at = datetime(2026, 1, 1, tzinfo=UTC)
    await seed_run(druks_db, kind=Summarize.kind, subject=note, state="finished")

    assert current_work(client)["rows"] == []


async def test_subjectless_and_orphaned_runs_each_stay_current(client, druks_db):
    background = await seed_run(druks_db, kind=Summarize.kind, state="running")
    orphan = Run(
        id=str(uuid7()), kind=Summarize.kind, created_at=datetime.now(UTC) - timedelta(minutes=10)
    )
    druks_db.add(orphan)
    await druks_db.flush()

    rows = {row["run"]: row for row in current_work(client)["rows"]}

    assert rows[background.id]["state"] == "running"
    assert rows[orphan.id]["state"] == "orphaned"
    assert rows[orphan.id]["subjectId"] is None


async def test_only_installed_workflow_kinds_are_read(client, druks_db):
    other = await Account.get_or_create("another@example.invalid")
    note = await Note.create(body="shared run")
    included = await seed_run(druks_db, kind=Summarize.kind, subject=note)
    included.account_id = other.id
    await seed_run(druks_db, kind="uninstalled.sweep")
    await druks_db.flush()

    assert [row["run"] for row in current_work(client)["rows"]] == [included.id]


async def test_changed_time_orders_current_work(client, druks_db):
    first = await seed_run(druks_db, kind=Summarize.kind, subject=await Note.create(body="first"))
    second = await seed_run(druks_db, kind=Summarize.kind, subject=await Note.create(body="second"))
    await druks_db.execute(
        workflow_status.update()
        .where(workflow_status.c.workflow_uuid == first.id)
        .values(updated_at=int((datetime.now(UTC) + timedelta(minutes=1)).timestamp() * 1000))
    )

    assert [row["run"] for row in current_work(client)["rows"]] == [first.id, second.id]


async def test_the_read_is_capped(client, druks_db, monkeypatch):
    monkeypatch.setattr(overview, "PAGE_SIZE", 1)
    for body in ("one", "two"):
        await seed_run(druks_db, kind=Summarize.kind, subject=await Note.create(body=body))

    body = current_work(client)

    assert len(body["rows"]) == 1
    assert body["hasMore"] is True


async def test_schedules_resolve_paused_override_and_operator_timezone(
    client, druks_db, monkeypatch
):
    monkeypatch.setattr(Summarize, "every", "0 9 * * *")
    await SettingsOverride.set_workflow_setting(Summarize.kind, "schedule", "15 10 * * 1")
    await SettingsOverride.set_workflow_setting(Summarize.kind, "schedule_enabled", False)
    await (await UserSettings.get()).update_profile(timezone="Europe/Madrid")

    response = client.get("/api/overview/schedules")

    assert response.status_code == 200
    assert {
        "app": "field_notes",
        "kind": Summarize.kind,
        "cron": "15 10 * * 1",
        "enabled": False,
        "timezone": "Europe/Madrid",
    } in response.json()["rows"]


def test_overview_requires_the_existing_identity_gate(tmp_path, druks_db):
    app = configure_app_for_test(
        settings=make_settings(tmp_path, identity={"mode": "header", "header": "X-Edge-Email"}),
        authenticated=False,
    )
    with TestClient(app) as anonymous:
        assert anonymous.get("/api/overview/work").status_code == 401
        assert anonymous.get("/api/overview/schedules").status_code == 401

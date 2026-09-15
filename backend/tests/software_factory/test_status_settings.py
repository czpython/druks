from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import httpx
import pytest
from conftest import connect_service, settings_client
from druks import doctor
from druks.contrib.software_factory.app import SoftwareFactory
from druks.contrib.software_factory.ticketing.jira import Jira
from druks.contrib.software_factory.ticketing.linear import Linear
from druks.database import db_session
from druks.testing import make_settings


@pytest.fixture(params=["linear", "jira"])
async def connected_tracker(request, monkeypatch, druks_db):
    await connect_service(
        request.param,
        identity={"base_url": "https://jira.test", "email": "operator@example.test"},
        secrets={"api_key": "test-key", "api_token": "test-token"},
    )
    await SoftwareFactory.override_setting("tracker", request.param)
    statuses = AsyncMock(
        return_value=[
            (name, name)
            for name in ("Ready for Agent", "In Progress", "In Review", "Done", "Backlog")
        ]
    )
    monkeypatch.setattr(
        {"linear": Linear, "jira": Jira}[request.param], "list_status_choices", statuses
    )
    return statuses


async def test_matching_statuses_pass(connected_tracker):
    assert await SoftwareFactory.get_settings_problems() == {}
    connected_tracker.assert_awaited_once()


@pytest.mark.parametrize(
    "field",
    ["trigger_status", "in_progress_status", "in_review_status", "done_status", "resting_status"],
)
async def test_unknown_status_names_are_field_errors(connected_tracker, field):
    await SoftwareFactory.override_setting(field, "ready for agent")

    problems = await SoftwareFactory.get_settings_problems()

    assert set(problems) == {field}
    assert "No tracker status matches 'ready for agent'" in problems[field]


async def test_optional_empty_statuses_pass(connected_tracker):
    await SoftwareFactory.override_setting("in_review_status", "")
    await SoftwareFactory.override_setting("resting_status", "")

    assert await SoftwareFactory.get_settings_problems() == {}


async def test_tracker_selection_can_be_saved_before_choosing_its_statuses(connected_tracker):
    await SoftwareFactory.override_setting("trigger_status", "Status from another tracker")
    assert await SoftwareFactory.get_settings_problems(fields={"tracker"}) == {}
    connected_tracker.assert_not_awaited()
    assert "trigger_status" in await SoftwareFactory.get_settings_problems()


@pytest.mark.parametrize("field", ["trigger_status", "in_progress_status", "done_status"])
async def test_required_empty_statuses_are_field_errors(connected_tracker, field):
    await SoftwareFactory.override_setting(field, "")

    assert set(await SoftwareFactory.get_settings_problems()) == {field}


async def test_failed_lookup_does_not_claim_that_status_names_are_invalid(connected_tracker):
    connected_tracker.side_effect = httpx.ConnectError("private provider detail")

    assert await SoftwareFactory.get_settings_problems() == {
        "tracker": "Could not read tracker statuses. Check the connection and retry."
    }


@pytest.mark.parametrize("tracker", ["none", "druks", "linear", "jira"])
async def test_status_validation_allows_setup_without_a_connected_external_tracker(tracker):
    await SoftwareFactory.override_setting("tracker", tracker)
    await SoftwareFactory.override_setting("trigger_status", "Unconfigured")

    assert await SoftwareFactory.get_settings_problems() == {}


async def test_save_rejects_an_unknown_status_and_keeps_the_saved_value(
    connected_tracker, tmp_path
):
    with settings_client(tmp_path) as client:
        rejected = client.patch(
            "/api/settings/apps",
            json={"appSettings": {"software_factory": {"trigger_status": "ready for agent"}}},
        )
        assert rejected.status_code == 422
        assert "trigger_status" in rejected.json()["detail"]["software_factory"]
        app = next(
            app
            for app in client.get("/api/settings/apps").json()["apps"]
            if app["name"] == "software_factory"
        )
        field = next(field for field in app["settings"] if field["name"] == "trigger_status")
        assert field["value"] == "Ready for Agent"


async def test_doctor_reports_an_unknown_saved_status(
    connected_tracker, tmp_path, monkeypatch, druks_db
):
    await SoftwareFactory.override_setting("trigger_status", "ready for agent")

    @asynccontextmanager
    async def engine(_settings):
        yield druks_db.bind

    monkeypatch.setattr(doctor, "_check_engine", engine)
    monkeypatch.setattr(doctor, "iter_apps", lambda: iter([SoftwareFactory]))
    try:
        results = await doctor.check_apps(make_settings(tmp_path))
    finally:
        db_session.registry.set(druks_db)

    result = next(result for result in results if result.name == "software_factory:settings")
    assert not result.ok
    assert "Trigger status: No tracker status matches" in result.detail

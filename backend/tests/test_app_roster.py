from pathlib import Path

from druks.testing import configure_app_for_test, make_settings
from fastapi.testclient import TestClient


def test_roster_lists_installed_apps_with_subject_types(tmp_path: Path):
    with TestClient(configure_app_for_test(settings=make_settings(tmp_path))) as client:
        roster = {entry["name"]: entry for entry in client.get("/api/apps").json()}

    software_factory = roster["software_factory"]
    assert software_factory["builtin"] is False
    assert "work_item" in software_factory["subjectTypes"]
    assert software_factory["hasFrontend"] is False
    # Software Factory's pages are React, so its tabs live in its frontend.
    assert software_factory["navigation"] == []
    assert software_factory["icon"]
    issues = roster["issues"]
    assert issues["builtin"] is False
    assert issues["hasFrontend"] is False
    assert issues["icon"] == "layers"
    # No workflow yet — subjects follow from workflows, not from StoredSubject alone.
    assert issues["subjectTypes"] == []

    field_notes = roster["field_notes"]
    assert field_notes["subjectTypes"] == ["note"]
    # Derived from the landing page the app declares, labelled by that page.
    assert field_notes["navigation"] == [["/field_notes", "notes"]]

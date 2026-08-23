from pathlib import Path

from druks.testing import configure_app_for_test, make_settings
from fastapi.testclient import TestClient


def test_roster_lists_installed_apps_with_subject_types(tmp_path: Path):
    with TestClient(configure_app_for_test(settings=make_settings(tmp_path))) as client:
        roster = {entry["name"]: entry for entry in client.get("/api/apps").json()}

    ship = roster["ship"]
    assert ship["builtin"] is False
    assert "work_item" in ship["subjectTypes"]
    assert ship["hasFrontend"] is False
    assert ship["navigation"] == [
        ["/ship", "active"],
        ["/ship/history", "history"],
        ["/ship/projects", "projects"],
    ]
    assert ship["icon"]
    field_notes = roster["field_notes"]
    assert field_notes["subjectTypes"] == ["note"]
    assert field_notes["navigation"] == [["/field_notes", "notes"]]

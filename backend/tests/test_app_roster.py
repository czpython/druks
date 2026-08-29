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
    assert software_factory["navigation"] == [
        ["/software_factory", "active"],
        ["/software_factory/history", "history"],
        ["/software_factory/projects", "projects"],
    ]
    assert software_factory["icon"]
    field_notes = roster["field_notes"]
    assert field_notes["subjectTypes"] == ["note"]
    assert field_notes["navigation"] == [["/field_notes", "notes"]]

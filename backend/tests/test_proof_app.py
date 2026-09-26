from druks.apps.loader import load_app
from druks.models import Base

_PACKAGE = "druks_field_notes"


def test_boot_loads_the_external_app():
    app = load_app("field_notes")

    assert app.name == "field_notes"
    assert app.package == _PACKAGE
    assert [subject.__name__ for subject in app.subjects()] == ["Note", "Repository"]


def test_discovery_registers_the_tables_and_capabilities():
    app = load_app("field_notes")

    assert "field_notes_note" in Base.metadata.tables
    assert [workflow.__name__ for workflow in app.workflows()] == ["Summarize", "Survey"]

    prefixes = {router.prefix for router in app.routers()}
    assert prefixes >= {"/notes", "/transcripts/{call_id}", "/note"}


def test_migration_is_the_history_root():
    app = load_app("field_notes")

    package_dir = app.package_dir()
    assert app.migrations_dir() == package_dir / "migrations"
    baseline, *later = sorted((package_dir / "migrations" / "versions").glob("*.py"))
    assert baseline.name.startswith("field_notes_0001")
    assert all(path.name.startswith("field_notes_") for path in later)

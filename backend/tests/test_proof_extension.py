from druks.extensions.loader import load_extension
from druks.models import Base

_PACKAGE = "druks_field_notes"


def test_boot_loads_the_external_extension():
    extension = load_extension("field_notes")

    assert extension.name == "field_notes"
    assert extension.package == _PACKAGE
    assert [subject.__name__ for subject in extension.subjects()] == ["Note"]


def test_discovery_registers_the_tables_and_capabilities():
    extension = load_extension("field_notes")

    assert "field_notes_notes" in Base.metadata.tables
    assert [workflow.__name__ for workflow in extension.workflows()] == ["Summarize"]

    capability_modules = {module.__name__ for module in extension.capability_modules()}
    assert f"{_PACKAGE}.subscribers" in capability_modules
    prefixes = {router.prefix for router in extension.routers()}
    assert prefixes >= {"/notes", "/transcripts/{call_id}", "/note"}


def test_migration_is_the_history_root():
    extension = load_extension("field_notes")

    package_dir = extension.package_dir()
    assert extension.migrations_dir() == package_dir / "migrations"
    (baseline,) = (package_dir / "migrations" / "versions").glob("*.py")
    assert baseline.name.startswith("field_notes_")

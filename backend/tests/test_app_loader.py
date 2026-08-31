from pathlib import Path

_PLATFORM_ROOT = Path(__file__).resolve().parent.parent / "druks"


def test_import_app_models_registers_software_factory_via_generic_discovery():
    # SoftwareFactory's tables are unprefixed (they live in core's schema), so it flows through the
    # same iter_apps() path as any app — exempt via prefix_tables=False, not a
    # hardcoded platform import.
    from druks.apps.loader import get_app, import_app_models
    from druks.models import Base

    assert get_app("software_factory").prefix_tables is False
    import_app_models()  # idempotent; raises if the unprefixed tables aren't exempt
    assert {"projects", "work_items", "project_repos"} <= set(Base.metadata.tables)


def test_platform_does_not_import_an_app_package():
    # Regression guard for the inverted dependency: the loader and the db bootstrap must
    # not name an app — apps register through discovery, so removing or
    # unbundling one can't break init_db.
    for module in ("apps/loader.py", "database.py"):
        source = (_PLATFORM_ROOT / module).read_text()
        assert "import druks.contrib" not in source, f"{module} imports the contrib namespace"
        assert "import druks.usage" not in source, f"{module} imports the usage app"

from pathlib import Path

_PLATFORM_ROOT = Path(__file__).resolve().parent.parent / "druks"


def test_import_extension_models_registers_ship_via_generic_discovery():
    # Ship's tables are unprefixed (they live in core's schema), so it flows through the
    # same iter_extensions() path as any extension — exempt via prefix_tables=False, not a
    # hardcoded platform import.
    from druks.contrib.ship.extension import Ship
    from druks.extensions.loader import import_extension_models
    from druks.models import Base

    assert Ship.prefix_tables is False
    import_extension_models()  # idempotent; raises if the unprefixed tables aren't exempt
    assert {"projects", "work_items", "project_repos"} <= set(Base.metadata.tables)


def test_platform_does_not_import_an_extension_package():
    # Regression guard for the inverted dependency: the loader and the db bootstrap must
    # not name an extension — extensions register through discovery, so removing or
    # unbundling one can't break init_db.
    for module in ("extensions/loader.py", "database.py"):
        source = (_PLATFORM_ROOT / module).read_text()
        assert "import druks.contrib" not in source, f"{module} imports the contrib namespace"
        assert "import druks.usage" not in source, f"{module} imports the usage extension"

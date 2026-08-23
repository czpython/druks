import sys
import textwrap
from importlib.metadata import EntryPoint
from pathlib import Path

import pytest
from druks.apps import loader
from druks.apps.exceptions import (
    AppImportError,
    AppLoadError,
    AppNotFound,
    AppSubjectContractError,
    MalformedApp,
)
from druks.apps.loader import load_app

# An out-of-tree app package, written to disk and put on sys.path so a real
# importlib.metadata.EntryPoint resolves it — the same machinery an editable
# ``pip install -e`` would wire, without mutating the shared environment's
# installed dist metadata. The whole point is a package that lives outside the
# druks tree, loaded headlessly. Built once per module so its ``Base`` model is
# declared exactly once (re-declaring a mapped class into the shared metadata
# collides); the per-test loads are idempotent re-imports.
_PACKAGE = "druks_probe"
_FILES = {
    "app.py": """
        from druks.apps import App, AppSettings
        from pydantic import Field


        class Probe(App):
            name = "probe"

            class Settings(AppSettings):
                budget: int = Field(default=3, ge=1)
    """,
    "models.py": """
        from druks.db import StoredSubject
        from druks.durable.schemas import SubjectSummary


        class ProbeItem(StoredSubject):
            __tablename__ = "probe_items"

            @classmethod
            def list_summaries(cls, account_id: str | None) -> list[SubjectSummary]:
                return []
    """,
    "routes.py": """
        from fastapi import APIRouter

        router = APIRouter(prefix="/widgets")


        @router.get("")
        def list_widgets() -> list[str]:
            return []
    """,
    "subscribers.py": """
        from druks.signals import subscribe

        from .models import ProbeItem


        @subscribe("workflow.finished", subject__type="probe_item")
        async def on_probe_item_done(*, subject: ProbeItem, **_: object) -> None:
            ...
    """,
    "workflows.py": """
        from druks.workflows import Workflow

        from .models import ProbeItem


        class Inspect(Workflow):
            subject = ProbeItem

            async def run(self, widget: str) -> None:
                ...
    """,
    "services.py": """
        from druks.services import Service
        from pydantic import BaseModel, Field, SecretStr


        class Probemail(Service):

            class Settings(BaseModel):
                account: str = Field(title="Account")
                api_key: SecretStr = Field(title="API key")
    """,
}


# A package whose declared ``StoredSubject`` omits ``list_summaries()``. Its own
# table keeps it off the conforming probe's shared metadata.
_BROKEN_STORED_FILES = {
    "app.py": """
        from druks.apps import App


        class BrokenStored(App):
            name = "brokenstored"
    """,
    "models.py": """
        from druks.db import StoredSubject


        class Ledger(StoredSubject):
            __tablename__ = "brokenstored_ledgers"
            # No list_summaries() — the load gate must reject it.
    """,
    "workflows.py": """
        from druks.workflows import Workflow

        from .models import Ledger


        class Post(Workflow):
            subject = Ledger

            async def run(self) -> None:
                ...
    """,
}


def _write_package(root: Path, package: str, files: dict[str, str]) -> None:
    directory = root / package
    (directory / "migrations" / "versions").mkdir(parents=True)
    (directory / "__init__.py").write_text("")
    for name, body in files.items():
        (directory / name).write_text(textwrap.dedent(body))


def _entry(package: str) -> EntryPoint:
    return EntryPoint(name="probe", value=f"{package}.app:Probe", group="druks.apps")


@pytest.fixture(scope="module")
def external_app(tmp_path_factory):
    """Build the probe package, expose it as the sole installed ``druks.apps``
    entry point, and restore every global its load mutates (registries, table
    metadata, signal receivers) so the suite stays clean."""
    from blinker import signal
    from druks.apps import loader as apps_loader
    from druks.apps.registry import agents, services, webhooks, workflows
    from druks.models import Base

    root = tmp_path_factory.mktemp("external")
    _write_package(root, _PACKAGE, _FILES)
    sys.path.insert(0, str(root))

    tables = set(Base.metadata.tables)
    registries = {
        registry: dict(registry._items) for registry in (agents, services, webhooks, workflows)
    }
    packages = dict(apps_loader._workflow_packages)
    finished = signal("workflow.finished")
    receivers = dict(finished.receivers)
    try:
        yield _entry(_PACKAGE)
    finally:
        sys.path.remove(str(root))
        for name in set(Base.metadata.tables) - tables:
            Base.metadata.remove(Base.metadata.tables[name])
        for registry, snapshot in registries.items():
            registry._items = snapshot
        apps_loader._workflow_packages.clear()
        apps_loader._workflow_packages.update(packages)
        finished.receivers = receivers
        for name in [m for m in sys.modules if m == _PACKAGE or m.startswith(f"{_PACKAGE}.")]:
            del sys.modules[name]


@pytest.fixture
def installed(external_app, monkeypatch):
    """The probe entry point as the only one the loader sees."""
    monkeypatch.setattr(loader, "entry_points", lambda *, group: [external_app])
    return external_app


def test_loads_an_external_app_without_the_web_app(installed):
    """An out-of-tree, entry-point-declared app loads with no FastAPI app."""
    app = load_app("probe")

    assert app.name == "probe"
    assert app.package == _PACKAGE


def test_load_registers_the_apps_tables(installed):
    """Loading imports the app's models, registering its prefixed tables."""
    from druks.models import Base

    load_app("probe")

    assert "probe_items" in Base.metadata.tables


def test_surfaces_are_enumerable_from_the_loaded_app(installed):
    """Workflows, routes, subscribers, settings, and migrations all read off the
    loaded app without booting the platform."""
    app = load_app("probe")

    assert [workflow.__name__ for workflow in app.workflows()] == ["Inspect"]

    router_prefixes = {router.prefix for router in app.routers()}
    assert "/widgets" in router_prefixes  # the app's own router
    assert "/transcripts/{call_id}" in router_prefixes  # the free read-side
    # Its one workflow declares ProbeItem, so the subject read-side mounts too.
    assert "/probe_item" in router_prefixes

    capability_modules = {module.__name__ for module in app.capability_modules()}
    assert f"{_PACKAGE}.subscribers" in capability_modules

    settings_model = app.settings_model
    assert settings_model
    assert list(settings_model.model_fields) == ["budget"]

    package_dir = app.package_dir()
    assert package_dir
    assert app.migrations_dir() == package_dir / "migrations"


def test_load_registers_the_apps_services(installed):
    """The ``services`` module is a discovered role: its declarations register on load."""
    from druks.apps.registry import services

    load_app("probe")

    assert services.get("probemail").title == "Probemail"


def test_missing_package_raises_app_not_found(installed):
    """A name no installed package declares fails as AppNotFound."""
    with pytest.raises(AppNotFound, match="no installed app named 'ghost'"):
        load_app("ghost")


def test_named_failures_share_one_load_error_base(installed):
    """Every load failure is catchable as AppLoadError — one except for callers."""
    with pytest.raises(AppLoadError):
        load_app("ghost")


def test_malformed_entry_point_raises_malformed_app(monkeypatch):
    """An entry point resolving to a non-App fails as MalformedApp."""
    entry = EntryPoint(name="bad", value="builtins:object", group="druks.apps")
    monkeypatch.setattr(loader, "entry_points", lambda *, group: [entry])

    with pytest.raises(MalformedApp, match="is not an App"):
        load_app("bad")


def test_duplicate_entry_points_raise_malformed_app(monkeypatch):
    """Two installed packages claiming one name is a broken install — the loader
    fails loudly rather than silently loading an arbitrary one."""
    duplicates = [
        EntryPoint(name="probe", value="one.app:Probe", group="druks.apps"),
        EntryPoint(name="probe", value="two.app:Probe", group="druks.apps"),
    ]
    monkeypatch.setattr(loader, "entry_points", lambda *, group: duplicates)

    with pytest.raises(MalformedApp, match="declared by 2 installed packages"):
        load_app("probe")


def test_load_does_not_import_sibling_apps(installed, monkeypatch):
    """Loading one app imports only its own package — a single-app load
    must not pull sibling entry modules and pollute the global registries."""
    sibling = EntryPoint(name="other", value="druks_never_imported.app:Other", group="druks.apps")
    monkeypatch.setattr(loader, "entry_points", lambda *, group: [installed, sibling])

    load_app("probe")

    assert "druks_never_imported" not in sys.modules
    assert "druks_never_imported.app" not in sys.modules


def test_unresolvable_entry_point_target_raises_malformed_app(monkeypatch):
    """An entry point whose target attribute doesn't exist fails as MalformedApp,
    not as a raw AttributeError leaking from importlib."""
    entry = EntryPoint(name="bad", value="druks.apps.base:NoSuchClass", group="druks.apps")
    monkeypatch.setattr(loader, "entry_points", lambda *, group: [entry])

    with pytest.raises(MalformedApp, match="doesn't define"):
        load_app("bad")


def test_missing_entry_module_raises_malformed_app(monkeypatch):
    """An entry point pointing at a module that isn't installed is a packaging
    mistake — MalformedApp, not AppImportError."""
    entry = EntryPoint(name="bad", value="not_installed_pkg.app:X", group="druks.apps")
    monkeypatch.setattr(loader, "entry_points", lambda *, group: [entry])

    with pytest.raises(MalformedApp, match="isn't installed"):
        load_app("bad")


def test_entry_point_key_mismatch_raises_malformed_app(installed, monkeypatch):
    """An entry-point key that doesn't equal the class's ``name`` — the key scopes
    the namespaces, so a mismatch is malformed."""
    aliased = EntryPoint(name="not_probe", value=f"{_PACKAGE}.app:Probe", group="druks.apps")
    monkeypatch.setattr(loader, "entry_points", lambda *, group: [aliased])

    with pytest.raises(MalformedApp, match="must match App.name"):
        load_app("not_probe")


def test_boot_rejects_an_entry_point_key_mismatch(installed, monkeypatch):
    """Full boot applies the same key/name validation as the single load — an
    aliased entry the headless path rejects must not slip through iter_apps()."""
    aliased = EntryPoint(name="not_probe", value=f"{_PACKAGE}.app:Probe", group="druks.apps")
    monkeypatch.setattr(loader, "entry_points", lambda *, group: [aliased])

    with pytest.raises(MalformedApp, match="must match App.name"):
        loader.iter_apps()


def test_import_error_in_entry_module_raises_app_import_error(tmp_path, monkeypatch):
    """The app's entry module raising on import (e.g. a missing dependency it
    imports) surfaces as AppImportError — the app's code failed, distinct
    from a packaging target mistake."""
    package = "druks_import_boom"
    directory = tmp_path / package
    (directory / "migrations" / "versions").mkdir(parents=True)
    (directory / "__init__.py").write_text("")
    (directory / "app.py").write_text(
        "import totally_absent_dependency  # noqa: F401\n"
        "from druks.apps import App\n\n\n"
        "class Boom(App):\n    name = 'boom'\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    entry = EntryPoint(name="boom", value=f"{package}.app:Boom", group="druks.apps")
    monkeypatch.setattr(loader, "entry_points", lambda *, group: [entry])

    with pytest.raises(AppImportError, match="entry module") as caught:
        load_app("boom")
    assert isinstance(caught.value.__cause__, ModuleNotFoundError)


def test_import_error_in_models_raises_app_import_error(tmp_path, monkeypatch):
    """A well-declared app whose models module raises on import surfaces as
    AppImportError, carrying the original exception as its cause."""
    package = "druks_broken_probe"
    files = {**_FILES, "models.py": "raise RuntimeError('boom on import')\n"}
    _write_package(tmp_path, package, files)
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(loader, "entry_points", lambda *, group: [_entry(package)])

    with pytest.raises(AppImportError, match="failed to import") as caught:
        load_app("probe")
    assert isinstance(caught.value.__cause__, RuntimeError)


@pytest.fixture
def broken_stored_app(tmp_path_factory, monkeypatch):
    """The broken package as the only installed entry point; restores the globals
    its load mutates."""
    from druks.apps import loader as apps_loader
    from druks.apps.registry import agents, services, webhooks, workflows
    from druks.models import Base

    package = "druks_broken_stored"
    root = tmp_path_factory.mktemp("broken_stored")
    _write_package(root, package, _BROKEN_STORED_FILES)
    sys.path.insert(0, str(root))

    tables = set(Base.metadata.tables)
    registries = {
        registry: dict(registry._items) for registry in (agents, services, webhooks, workflows)
    }
    packages = dict(apps_loader._workflow_packages)
    entry = EntryPoint(name="brokenstored", value=f"{package}.app:BrokenStored", group="druks.apps")
    monkeypatch.setattr(loader, "entry_points", lambda *, group: [entry])
    try:
        yield
    finally:
        sys.path.remove(str(root))
        for name in set(Base.metadata.tables) - tables:
            Base.metadata.remove(Base.metadata.tables[name])
        for registry, snapshot in registries.items():
            registry._items = snapshot
        apps_loader._workflow_packages.clear()
        apps_loader._workflow_packages.update(packages)
        for name in [m for m in sys.modules if m == package or m.startswith(f"{package}.")]:
            del sys.modules[name]


def test_headless_load_rejects_a_stored_subject_missing_list_summaries(broken_stored_app):
    """A row-backed subject is gated the same as ``Subject`` — typed, not an import error."""
    with pytest.raises(AppSubjectContractError) as caught:
        load_app("brokenstored")
    message = str(caught.value)
    assert "brokenstored" in message  # the app name
    assert "Ledger" in message  # the subject class
    assert "list_summaries()" in message  # the missing method
    assert "Implement list_summaries()" in message  # the implementation direction
    assert isinstance(caught.value, AppLoadError)

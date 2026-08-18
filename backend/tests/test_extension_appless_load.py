import sys
import textwrap
from importlib.metadata import EntryPoint
from pathlib import Path

import pytest
from druks.extensions import loader
from druks.extensions.exceptions import (
    ExtensionImportError,
    ExtensionLoadError,
    ExtensionNotFound,
    ExtensionSubjectContractError,
    MalformedExtension,
)
from druks.extensions.loader import load_extension

# An out-of-tree extension package, written to disk and put on sys.path so a real
# importlib.metadata.EntryPoint resolves it — the same machinery an editable
# ``pip install -e`` would wire, without mutating the shared environment's
# installed dist metadata. The whole point is a package that lives outside the
# druks tree, loaded app-lessly. Built once per module so its ``Base`` model is
# declared exactly once (re-declaring a mapped class into the shared metadata
# collides); the per-test loads are idempotent re-imports.
_PACKAGE = "druks_probe"
_FILES = {
    "extension.py": """
        from druks.extensions import Extension, ExtensionSettings
        from pydantic import Field


        class Probe(Extension):
            name = "probe"

            class Settings(ExtensionSettings):
                budget: int = Field(default=3, ge=1)
    """,
    "models.py": """
        from druks.db import StoredSubject
        from druks.durable.schemas import SubjectSummary


        class ProbeItem(StoredSubject):
            __tablename__ = "probe_items"

            @classmethod
            def list_summaries(cls) -> list[SubjectSummary]:
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
            name = "probemail"
            title = "Probemail"

            class Settings(BaseModel):
                account: str = Field(title="Account")
                api_key: SecretStr = Field(title="API key")
    """,
}


# A second on-disk package whose declared ``StoredSubject`` omits ``list_summaries()``.
# Its own package name and table keep it off the conforming probe's shared metadata, so
# the broken StoredSubject case can load app-lessly without colliding with ``ProbeItem``.
_BROKEN_STORED_FILES = {
    "extension.py": """
        from druks.extensions import Extension


        class BrokenStored(Extension):
            name = "brokenstored"
    """,
    "models.py": """
        from druks.db import StoredSubject


        class Ledger(StoredSubject):
            __tablename__ = "brokenstored_ledgers"
            # Intentionally omits list_summaries(): a workflow declares it, so the
            # load-time contract must reject it.
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
    return EntryPoint(name="probe", value=f"{package}.extension:Probe", group="druks.extensions")


@pytest.fixture(scope="module")
def external_extension(tmp_path_factory):
    """Build the probe package, expose it as the sole installed ``druks.extensions``
    entry point, and restore every global its load mutates (registries, table
    metadata, signal receivers) so the suite stays clean."""
    from blinker import signal
    from druks.extensions import loader as extensions_loader
    from druks.extensions.registry import agents, services, webhooks, workflows
    from druks.models import Base

    root = tmp_path_factory.mktemp("external")
    _write_package(root, _PACKAGE, _FILES)
    sys.path.insert(0, str(root))

    tables = set(Base.metadata.tables)
    registries = {r: dict(r._items) for r in (agents, services, webhooks, workflows)}
    packages = dict(extensions_loader._workflow_packages)
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
        extensions_loader._workflow_packages.clear()
        extensions_loader._workflow_packages.update(packages)
        finished.receivers = receivers
        for name in [m for m in sys.modules if m == _PACKAGE or m.startswith(f"{_PACKAGE}.")]:
            del sys.modules[name]


@pytest.fixture
def installed(external_extension, monkeypatch):
    """The probe entry point as the only one the loader sees."""
    monkeypatch.setattr(loader, "entry_points", lambda *, group: [external_extension])
    return external_extension


def test_loads_an_external_extension_without_the_web_app(installed):
    """An out-of-tree, entry-point-declared extension loads with no FastAPI app."""
    extension = load_extension("probe")

    assert extension.name == "probe"
    assert extension.package == _PACKAGE


def test_load_registers_the_extensions_tables(installed):
    """Loading imports the extension's models, registering its prefixed tables."""
    from druks.models import Base

    load_extension("probe")

    assert "probe_items" in Base.metadata.tables


def test_surfaces_are_enumerable_from_the_loaded_extension(installed):
    """Workflows, routes, subscribers, settings, and migrations all read off the
    loaded extension without booting the platform."""
    extension = load_extension("probe")

    assert [workflow.__name__ for workflow in extension.workflows()] == ["Inspect"]

    router_prefixes = {router.prefix for router in extension.routers()}
    assert "/widgets" in router_prefixes  # the extension's own router
    assert "/transcripts/{call_id}" in router_prefixes  # the free read-side
    # Its one workflow declares ProbeItem, so the subject read-side mounts too.
    assert "/probe_item" in router_prefixes

    capability_modules = {module.__name__ for module in extension.capability_modules()}
    assert f"{_PACKAGE}.subscribers" in capability_modules

    settings_model = extension.settings_model
    assert settings_model is not None
    assert list(settings_model.model_fields) == ["budget"]

    package_dir = extension.package_dir()
    assert package_dir is not None
    assert extension.migrations_dir() == package_dir / "migrations"


def test_load_registers_the_extensions_services(installed):
    """The ``services`` module is a discovered role: its declarations register on load."""
    from druks.extensions.registry import services

    load_extension("probe")

    assert services.get("probemail").title == "Probemail"


def test_missing_package_raises_extension_not_found(installed):
    """A name no installed package declares fails as ExtensionNotFound."""
    with pytest.raises(ExtensionNotFound, match="no installed extension named 'ghost'"):
        load_extension("ghost")


def test_named_failures_share_one_load_error_base(installed):
    """Every load failure is catchable as ExtensionLoadError — one except for callers."""
    with pytest.raises(ExtensionLoadError):
        load_extension("ghost")


def test_malformed_entry_point_raises_malformed_extension(monkeypatch):
    """An entry point resolving to a non-Extension fails as MalformedExtension."""
    entry = EntryPoint(name="bad", value="builtins:object", group="druks.extensions")
    monkeypatch.setattr(loader, "entry_points", lambda *, group: [entry])

    with pytest.raises(MalformedExtension, match="is not an Extension"):
        load_extension("bad")


def test_duplicate_entry_points_raise_malformed_extension(monkeypatch):
    """Two installed packages claiming one name is a broken install — the loader
    fails loudly rather than silently loading an arbitrary one."""
    duplicates = [
        EntryPoint(name="probe", value="one.extension:Probe", group="druks.extensions"),
        EntryPoint(name="probe", value="two.extension:Probe", group="druks.extensions"),
    ]
    monkeypatch.setattr(loader, "entry_points", lambda *, group: duplicates)

    with pytest.raises(MalformedExtension, match="declared by 2 installed packages"):
        load_extension("probe")


def test_load_does_not_import_sibling_extensions(installed, monkeypatch):
    """Loading one extension imports only its own package — a single-extension load
    must not pull sibling entry modules and pollute the global registries."""
    sibling = EntryPoint(
        name="other", value="druks_never_imported.extension:Other", group="druks.extensions"
    )
    monkeypatch.setattr(loader, "entry_points", lambda *, group: [installed, sibling])

    load_extension("probe")

    assert "druks_never_imported" not in sys.modules
    assert "druks_never_imported.extension" not in sys.modules


def test_unresolvable_entry_point_target_raises_malformed_extension(monkeypatch):
    """An entry point whose target attribute doesn't exist fails as MalformedExtension,
    not as a raw AttributeError leaking from importlib."""
    entry = EntryPoint(
        name="bad", value="druks.extensions.base:NoSuchClass", group="druks.extensions"
    )
    monkeypatch.setattr(loader, "entry_points", lambda *, group: [entry])

    with pytest.raises(MalformedExtension, match="doesn't define"):
        load_extension("bad")


def test_missing_entry_module_raises_malformed_extension(monkeypatch):
    """An entry point pointing at a module that isn't installed is a packaging
    mistake — MalformedExtension, not ExtensionImportError."""
    entry = EntryPoint(name="bad", value="not_installed_pkg.extension:X", group="druks.extensions")
    monkeypatch.setattr(loader, "entry_points", lambda *, group: [entry])

    with pytest.raises(MalformedExtension, match="isn't installed"):
        load_extension("bad")


def test_entry_point_key_mismatch_raises_malformed_extension(installed, monkeypatch):
    """An entry-point key that doesn't equal the class's ``name`` — the key scopes
    the namespaces, so a mismatch is malformed."""
    aliased = EntryPoint(
        name="not_probe", value=f"{_PACKAGE}.extension:Probe", group="druks.extensions"
    )
    monkeypatch.setattr(loader, "entry_points", lambda *, group: [aliased])

    with pytest.raises(MalformedExtension, match="must match Extension.name"):
        load_extension("not_probe")


def test_boot_rejects_an_entry_point_key_mismatch(installed, monkeypatch):
    """Full boot applies the same key/name validation as the single load — an
    aliased entry the app-less path rejects must not slip through iter_extensions()."""
    aliased = EntryPoint(
        name="not_probe", value=f"{_PACKAGE}.extension:Probe", group="druks.extensions"
    )
    monkeypatch.setattr(loader, "entry_points", lambda *, group: [aliased])

    with pytest.raises(MalformedExtension, match="must match Extension.name"):
        loader.iter_extensions()


def test_import_error_in_entry_module_raises_extension_import_error(tmp_path, monkeypatch):
    """The extension's entry module raising on import (e.g. a missing dependency it
    imports) surfaces as ExtensionImportError — the extension's code failed, distinct
    from a packaging target mistake."""
    package = "druks_import_boom"
    directory = tmp_path / package
    (directory / "migrations" / "versions").mkdir(parents=True)
    (directory / "__init__.py").write_text("")
    (directory / "extension.py").write_text(
        "import totally_absent_dependency  # noqa: F401\n"
        "from druks.extensions import Extension\n\n\n"
        "class Boom(Extension):\n    name = 'boom'\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    entry = EntryPoint(name="boom", value=f"{package}.extension:Boom", group="druks.extensions")
    monkeypatch.setattr(loader, "entry_points", lambda *, group: [entry])

    with pytest.raises(ExtensionImportError, match="entry module") as caught:
        load_extension("boom")
    assert isinstance(caught.value.__cause__, ModuleNotFoundError)


def test_import_error_in_models_raises_extension_import_error(tmp_path, monkeypatch):
    """A well-declared extension whose models module raises on import surfaces as
    ExtensionImportError, carrying the original exception as its cause."""
    package = "druks_broken_probe"
    files = {**_FILES, "models.py": "raise RuntimeError('boom on import')\n"}
    _write_package(tmp_path, package, files)
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(loader, "entry_points", lambda *, group: [_entry(package)])

    with pytest.raises(ExtensionImportError, match="failed to import") as caught:
        load_extension("probe")
    assert isinstance(caught.value.__cause__, RuntimeError)


@pytest.fixture
def broken_stored_extension(tmp_path_factory, monkeypatch):
    """Build the broken-StoredSubject package, expose it as the sole installed entry
    point, and restore every global its load mutates so the suite stays clean."""
    from druks.extensions import loader as extensions_loader
    from druks.extensions.registry import agents, services, webhooks, workflows
    from druks.models import Base

    package = "druks_broken_stored"
    root = tmp_path_factory.mktemp("broken_stored")
    _write_package(root, package, _BROKEN_STORED_FILES)
    sys.path.insert(0, str(root))

    tables = set(Base.metadata.tables)
    registries = {r: dict(r._items) for r in (agents, services, webhooks, workflows)}
    packages = dict(extensions_loader._workflow_packages)
    entry = EntryPoint(
        name="brokenstored", value=f"{package}.extension:BrokenStored", group="druks.extensions"
    )
    monkeypatch.setattr(loader, "entry_points", lambda *, group: [entry])
    try:
        yield
    finally:
        sys.path.remove(str(root))
        for name in set(Base.metadata.tables) - tables:
            Base.metadata.remove(Base.metadata.tables[name])
        for registry, snapshot in registries.items():
            registry._items = snapshot
        extensions_loader._workflow_packages.clear()
        extensions_loader._workflow_packages.update(packages)
        for name in [m for m in sys.modules if m == package or m.startswith(f"{package}.")]:
            del sys.modules[name]


def test_appless_load_rejects_a_stored_subject_missing_list_summaries(broken_stored_extension):
    """The StoredSubject base family is gated identically to Subject: an app-less load of
    an extension whose declared row-backed subject omits ``list_summaries()`` raises the
    same typed, actionable failure rather than an ``ExtensionImportError``."""
    with pytest.raises(ExtensionSubjectContractError) as caught:
        load_extension("brokenstored")
    message = str(caught.value)
    assert "brokenstored" in message  # the extension name
    assert "Ledger" in message  # the subject class
    assert "list_summaries()" in message  # the missing method
    assert "Implement list_summaries()" in message  # the implementation direction
    # The same failure is catchable through the common load-error base.
    assert isinstance(caught.value, ExtensionLoadError)

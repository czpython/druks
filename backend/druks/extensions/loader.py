from importlib.metadata import entry_points
from typing import TYPE_CHECKING

from druks.extensions import Extension

from .exceptions import ExtensionImportError, ExtensionNotFound, MalformedExtension

if TYPE_CHECKING:
    from importlib.metadata import EntryPoint
    from types import ModuleType

    from fastapi import FastAPI

_GROUP = "druks.extensions"

# Which extension owns each workflow-declaring package — the loader-validated
# installation claims, stamped once an entry point checks out, so a Workflow
# class resolves its identity at definition time. None marks a package whose
# workflows belong to no extension (how test modules register themselves).
_workflow_packages: dict[str, str | None] = {}


def register_workflow_package(package: str, extension: str | None) -> None:
    # Conflicting or overlapping claims are a packaging mistake — two installs
    # can't share a workflow package.
    if package in _workflow_packages:
        if _workflow_packages[package] != extension:
            raise MalformedExtension(
                f"package {package!r} already belongs to "
                f"{_workflow_packages[package]!r} — {extension!r} can't claim it"
            )
        return
    for registered, owner in _workflow_packages.items():
        nested = registered.startswith(f"{package}.") or package.startswith(f"{registered}.")
        if nested and owner != extension:
            raise MalformedExtension(
                f"package {package!r} overlaps {registered!r} (owned by {owner!r}) — "
                "workflow ownership must be unambiguous"
            )
    _workflow_packages[package] = extension


def resolve_workflow_extension(module: str) -> str | None:
    """The extension owning ``module``'s nearest registered ancestor package.
    Raises ``LookupError`` when no registered package contains the module."""
    prefix = module
    while prefix:
        if prefix in _workflow_packages:
            return _workflow_packages[prefix]
        prefix = prefix.rpartition(".")[0]
    raise LookupError(module)


def iter_extensions() -> list[type[Extension]]:
    """Every installed extension, resolved from the ``druks.extensions`` entry points.
    Loading an entry point imports the extension's class. An extension that fails to
    import, resolves to a non-``Extension``, or collides on ``name`` raises — there
    is no per-extension fault tolerance yet (deferred until a real external extension
    exists)."""
    extensions: list[type[Extension]] = []
    seen: set[str] = set()
    for entry in entry_points(group=_GROUP):
        extension = entry.load()
        if not (isinstance(extension, type) and issubclass(extension, Extension)):
            raise TypeError(f"extension entry point {entry.name!r} is not an Extension")
        if extension.name != entry.name:
            raise MalformedExtension(
                f"extension {entry.name!r} entry point resolves to an Extension named "
                f"{extension.name!r} — the entry-point name must match Extension.name"
            )
        if extension.name in seen:
            raise ValueError(f"duplicate extension name {extension.name!r}")
        seen.add(extension.name)
        # Ownership registers before discover() imports the capability modules,
        # whose Workflow classes resolve their extension at definition.
        register_workflow_package(extension.package, extension.name)
        extensions.append(extension)
    return extensions


def get_extension(name: str) -> type[Extension]:
    for extension in iter_extensions():
        if extension.name == name:
            return extension
    raise KeyError(f"no installed extension named {name!r}")


def load_extension(name: str) -> type[Extension]:
    """Load one installed extension without the web app: resolve its entry point,
    register its tables, and import its capability modules so its workflows,
    routes, subscribers, and webhooks self-register. Returns the ``Extension``
    class, with every surface then enumerable off it (``workflows()``,
    ``routers()``, ``capability_modules()``, ``settings_model``,
    ``migrations_dir()``). The load path used by the CLI, tests, and evals —
    no FastAPI, nothing mounted.

    Fails loudly and by name: an uninstalled package raises ``ExtensionNotFound``;
    an entry point that doesn't resolve to an ``Extension`` raises
    ``MalformedExtension``; the extension's own code raising on import raises
    ``ExtensionImportError``; a declared subject that fails the read-side contract
    raises ``ExtensionSubjectContractError``."""
    extension = _resolve(name)
    try:
        import_extension_models(extension)
        extension.discover()
    except Exception as error:
        raise ExtensionImportError(f"extension {name!r} failed to import: {error}") from error
    # Outside the try: a contract break is not an import error.
    extension.subjects()
    return extension


def _resolve(name: str) -> type[Extension]:
    """The single installed extension named ``name``, loaded to its ``Extension``
    class. Entry points are listed, not imported, so an unknown name fails before
    any extension code runs."""
    matches = [e for e in entry_points(group=_GROUP) if e.name == name]
    if not matches:
        raise ExtensionNotFound(
            f"no installed extension named {name!r} — install its package first"
        )
    if len(matches) > 1:
        # Two installed distributions register the same entry-point key — the name
        # keys the /api, settings, and migration namespaces, so this is a broken
        # install. Caught from metadata alone, without importing anything.
        raise MalformedExtension(
            f"extension {name!r} is declared by {len(matches)} installed packages "
            f"({', '.join(e.value for e in matches)}) — uninstall all but one"
        )
    extension = _load_entry(matches[0])
    # The entry-point key must equal the class's ``name`` — the key scopes the
    # /api, settings, and migration namespaces, which is what lets the
    # duplicate-key check above stand in for a duplicate-name check without
    # importing sibling extensions.
    if extension.name != name:
        raise MalformedExtension(
            f"extension {name!r} entry point resolves to an Extension named "
            f"{extension.name!r} — the entry-point name must match Extension.name"
        )
    register_workflow_package(extension.package, extension.name)
    return extension


def _load_entry(entry: "EntryPoint") -> type[Extension]:
    """Resolve one entry point to its ``Extension`` class, distinguishing a
    packaging mistake from the extension's own code failing on import. A missing
    target module or attribute is ``MalformedExtension`` (bad metadata); the
    target module existing but raising on import is ``ExtensionImportError`` (the
    extension's code) — the two the caller's taxonomy must tell apart."""
    import importlib

    try:
        module = importlib.import_module(entry.module)
    except ModuleNotFoundError as error:
        if error.name == entry.module or entry.module.startswith(f"{error.name}."):
            raise MalformedExtension(
                f"entry point {entry.value!r} points at a module that isn't installed: {error}"
            ) from error
        # A dependency the entry module imports is missing — its code ran and failed.
        raise ExtensionImportError(
            f"extension entry module {entry.module!r} failed to import: {error}"
        ) from error
    except Exception as error:
        raise ExtensionImportError(
            f"extension entry module {entry.module!r} failed to import: {error}"
        ) from error

    extension = module
    for attribute in filter(None, (entry.attr or "").split(".")):
        try:
            extension = getattr(extension, attribute)
        except AttributeError as error:
            raise MalformedExtension(
                f"entry point {entry.value!r} names {attribute!r}, which its module doesn't define"
            ) from error
    if not (isinstance(extension, type) and issubclass(extension, Extension)):
        raise MalformedExtension(f"entry point {entry.value!r} is not an Extension")
    return extension


def _tables_declared_in(package: str) -> set[str]:
    # A table belongs to whoever declared its model, not to whoever happened to import
    # it first: an extension entry module pulls its own models in, and a sibling's.
    from druks.models import Base

    return {
        mapper.local_table.name
        for mapper in Base.registry.mappers
        if mapper.class_.__module__.startswith(f"{package}.") and mapper.local_table is not None
    }


def import_extension_models(only: type[Extension] | None = None) -> None:
    """Import extensions' ``<package>.models`` so their tables register on the shared
    metadata before ``create_all`` or autogenerate. Defaults to every installed
    extension (boot, migrations); pass ``only`` to register a single extension's tables
    for an app-less load. A separately-shipped extension's tables must carry its
    ``<name>_`` prefix — the platform scopes its migrations by that prefix, so an
    unprefixed table would be invisible to them and fails the load instead. Builtin
    extensions are exempt: their schema is core's."""
    import importlib
    import importlib.util

    extensions = [only] if only else iter_extensions()
    for extension in extensions:
        name = f"{extension.package}.models"
        if not importlib.util.find_spec(name):
            continue
        importlib.import_module(name)
        misnamed = sorted(
            table
            for table in _tables_declared_in(extension.package)
            if not table.startswith(extension.table_prefix)
        )
        if misnamed and extension.prefix_tables and not extension.builtin:
            raise ValueError(
                f"extension {extension.name!r} tables must start with "
                f"{extension.table_prefix!r}: {misnamed}"
            )


def mount(app: "FastAPI", extension: type[Extension], modules: list["ModuleType"]) -> None:
    """Mount one discovered extension under ``/api/<name>`` and ``/app/<name>``.
    The prefix and the identity gate are the loader's — no extension hook can
    override them."""
    # Local, matching get_routers: the loader stays importable app-lessly.
    from fastapi import Depends

    from druks.accounts.dependencies import current_account

    prefix = f"/api/{extension.name}"
    for router in extension.get_routers(modules):
        app.include_router(
            router,
            prefix=prefix,
            tags=[extension.name],
            dependencies=[Depends(current_account)],
        )
    dist = extension.frontend_dist()
    if dist:
        # /app, not /api: unknown /api/* paths stay JSON 404s.
        app.frontend(f"/app/{extension.name}", directory=dist)


def load(app: "FastAPI") -> None:
    """API boot: for each extension — discover, validate subjects, mount."""
    # The table-prefix check runs here, not just in makemigrations — an author
    # who hand-writes migrations still can't boot with an unprefixed table.
    import_extension_models()
    for extension in iter_extensions():
        modules = extension.discover()
        extension.subjects()
        mount(app, extension, modules)

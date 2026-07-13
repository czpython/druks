from importlib.metadata import entry_points
from typing import TYPE_CHECKING

from druks.extensions import Extension

from .exceptions import ExtensionImportError, ExtensionNotFound, MalformedExtension
from .registry import claimed_workflow_package, register_workflow_package

if TYPE_CHECKING:
    from importlib.metadata import EntryPoint

    from fastapi import FastAPI

_GROUP = "druks.extensions"


def iter_extensions() -> list[type[Extension]]:
    """Every installed extension, resolved from the ``druks.extensions`` entry points.
    Loading an entry point imports the extension's class. An extension that fails to
    import, resolves to a non-``Extension``, or collides on ``name`` raises — there
    is no per-extension fault tolerance yet (deferred until a real external extension
    exists)."""
    entries = list(entry_points(group=_GROUP))
    # Ownership registers before any extension module imports, so every Workflow
    # class — including one the first entry's import pulls in — resolves its
    # declaring extension at definition time.
    for entry in entries:
        register_workflow_package(entry.module.rsplit(".", 1)[0], entry.name)
    extensions: list[type[Extension]] = []
    seen: set[str] = set()
    for entry in entries:
        extension = entry.load()
        if not (isinstance(extension, type) and issubclass(extension, Extension)):
            raise TypeError(f"extension entry point {entry.name!r} is not an Extension")
        if extension.name in seen:
            raise ValueError(f"duplicate extension name {extension.name!r}")
        seen.add(extension.name)
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
    no FastAPI, no ``load(app)``.

    Fails loudly and by name: an uninstalled package raises ``ExtensionNotFound``;
    an entry point that doesn't resolve to an ``Extension`` raises
    ``MalformedExtension``; the extension's own code raising on import raises
    ``ExtensionImportError``."""
    extension = _resolve(name)
    try:
        import_extension_models(extension)
        extension.discover()
    except Exception as error:
        raise ExtensionImportError(f"extension {name!r} failed to import: {error}") from error
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
    try:
        with claimed_workflow_package(matches[0].module.rsplit(".", 1)[0], name):
            extension = _load_entry(matches[0])
            # The entry-point key must equal the class's ``name``. That key is what
            # scopes the /api, settings, and migration namespaces here, so this is the
            # invariant that lets the duplicate-key check above stand in for a
            # duplicate-name check — without importing sibling extensions and defeating
            # the point of an app-less, single-extension load. A same-name collision
            # hidden behind a mismatched key is that sibling's own malformed state,
            # caught when it is loaded (or at full boot, where iter_extensions()
            # imports everything).
            if extension.name != name:
                raise MalformedExtension(
                    f"extension {name!r} entry point resolves to an Extension named "
                    f"{extension.name!r} — the entry-point name must match Extension.name"
                )
            register_workflow_package(extension.package, extension.name)
    except ValueError as error:
        # A claim the ownership map rejects (a nested-package overlap, one package
        # split across entry names) is a broken install — same taxonomy as a key
        # mismatch, caught here so the loader's error contract holds.
        raise MalformedExtension(str(error)) from error
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

    # Core/framework tables must be registered first, so an extension's transitive pull
    # of them isn't read as the extension's own (and flagged for missing the prefix).
    import druks.durable.models  # noqa: F401
    import druks.skills.models  # noqa: F401
    import druks.user_settings.models  # noqa: F401
    from druks.models import Base

    extensions = [only] if only else iter_extensions()
    seen = set(Base.metadata.tables)
    for extension in extensions:
        name = f"{extension.package}.models"
        if not importlib.util.find_spec(name):
            continue
        importlib.import_module(name)
        owned = set(Base.metadata.tables) - seen
        seen |= owned
        misnamed = sorted(t for t in owned if not t.startswith(extension.table_prefix))
        if misnamed and extension.prefix_tables and not extension.builtin:
            raise ValueError(
                f"extension {extension.name!r} tables must start with "
                f"{extension.table_prefix!r}: {misnamed}"
            )


def load(app: "FastAPI") -> None:
    """API boot entry: every extension imports its capabilities (self-registering its
    webhooks, workflows, agents, subscribers) and mounts its routers under
    ``/api/<name>``."""
    # The table-prefix check runs here, not just in makemigrations — an author
    # who hand-writes migrations still can't boot with an unprefixed table.
    import_extension_models()
    for extension in iter_extensions():
        extension.load(app)

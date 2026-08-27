from importlib.metadata import entry_points
from typing import TYPE_CHECKING

from druks.apps import App

from .exceptions import AppImportError, AppNotFound, MalformedApp

if TYPE_CHECKING:
    from importlib.metadata import EntryPoint
    from types import ModuleType

    from fastapi import FastAPI

_GROUP = "druks.apps"

# Which app owns each workflow-declaring package — the loader-validated
# installation claims, stamped once an entry point checks out, so a Workflow
# class resolves its identity at definition time.
_workflow_packages: dict[str, str] = {}


def register_workflow_package(package: str, app: str) -> None:
    # Conflicting or overlapping claims are a packaging mistake — two installs
    # can't share a workflow package.
    if package in _workflow_packages:
        if _workflow_packages[package] != app:
            raise MalformedApp(
                f"package {package!r} already belongs to "
                f"{_workflow_packages[package]!r} — {app!r} can't claim it"
            )
        return
    for registered, owner in _workflow_packages.items():
        nested = registered.startswith(f"{package}.") or package.startswith(f"{registered}.")
        if nested and owner != app:
            raise MalformedApp(
                f"package {package!r} overlaps {registered!r} (owned by {owner!r}) — "
                "workflow ownership must be unambiguous"
            )
    _workflow_packages[package] = app


def resolve_workflow_app(module: str) -> str:
    """The app owning ``module``'s nearest registered ancestor package.
    Raises ``LookupError`` when no registered package contains the module."""
    prefix = module
    while prefix:
        if prefix in _workflow_packages:
            return _workflow_packages[prefix]
        prefix = prefix.rpartition(".")[0]
    raise LookupError(module)


def iter_apps() -> list[type[App]]:
    """Every installed app, resolved from the ``druks.apps`` entry points.
    Loading an entry point imports the app's class. An app that fails to
    import, resolves to a non-``App``, or collides on ``name`` raises — there
    is no per-app fault tolerance yet (deferred until a real external app
    exists)."""
    apps: list[type[App]] = []
    seen: set[str] = set()
    for entry in entry_points(group=_GROUP):
        app = entry.load()
        if not (isinstance(app, type) and issubclass(app, App)):
            raise TypeError(f"app entry point {entry.name!r} is not an App")
        if app.name != entry.name:
            raise MalformedApp(
                f"app {entry.name!r} entry point resolves to an App named "
                f"{app.name!r} — the entry-point name must match App.name"
            )
        if app.name in seen:
            raise ValueError(f"duplicate app name {app.name!r}")
        seen.add(app.name)
        # Ownership registers before discover() imports the capability modules,
        # whose Workflow classes resolve their app at definition.
        register_workflow_package(app.package, app.name)
        apps.append(app)
    return apps


def get_app(name: str) -> type[App]:
    for app in iter_apps():
        if app.name == name:
            return app
    raise KeyError(f"no installed app named {name!r}")


def load_app(name: str) -> type[App]:
    """Load one installed app without the web app: resolve its entry point,
    register its tables, and import its capability modules so its workflows,
    routes, subscribers, and webhooks self-register. Returns the ``App``
    class, with every surface then enumerable off it (``workflows()``,
    ``routers()``, ``capability_modules()``, ``settings_model``,
    ``migrations_dir()``). The load path used by the CLI, tests, and evals —
    no FastAPI, nothing mounted.

    Fails loudly and by name: an uninstalled package raises ``AppNotFound``;
    an entry point that doesn't resolve to an ``App`` raises
    ``MalformedApp``; the app's own code raising on import raises
    ``AppImportError``; a declared subject that fails the read-side contract
    raises ``AppSubjectContractError``."""
    app = _resolve(name)
    try:
        import_app_models(app)
        app.discover()
    except Exception as error:
        raise AppImportError(f"app {name!r} failed to import: {error}") from error
    # Outside the try: a contract break is not an import error.
    app.subjects()
    return app


def _resolve(name: str) -> type[App]:
    """The single installed app named ``name``, loaded to its ``App``
    class. Entry points are listed, not imported, so an unknown name fails before
    any app code runs."""
    matches = [e for e in entry_points(group=_GROUP) if e.name == name]
    if not matches:
        raise AppNotFound(f"no installed app named {name!r} — install its package first")
    if len(matches) > 1:
        # Two installed distributions register the same entry-point key — the name
        # keys the /api, settings, and migration namespaces, so this is a broken
        # install. Caught from metadata alone, without importing anything.
        raise MalformedApp(
            f"app {name!r} is declared by {len(matches)} installed packages "
            f"({', '.join(e.value for e in matches)}) — uninstall all but one"
        )
    app = _load_entry(matches[0])
    # The entry-point key must equal the class's ``name`` — the key scopes the
    # /api, settings, and migration namespaces, which is what lets the
    # duplicate-key check above stand in for a duplicate-name check without
    # importing sibling apps.
    if app.name != name:
        raise MalformedApp(
            f"app {name!r} entry point resolves to an App named "
            f"{app.name!r} — the entry-point name must match App.name"
        )
    register_workflow_package(app.package, app.name)
    return app


def _load_entry(entry: "EntryPoint") -> type[App]:
    """Resolve one entry point to its ``App`` class, distinguishing a
    packaging mistake from the app's own code failing on import. A missing
    target module or attribute is ``MalformedApp`` (bad metadata); the
    target module existing but raising on import is ``AppImportError`` (the
    app's code) — the two the caller's taxonomy must tell apart."""
    import importlib

    try:
        module = importlib.import_module(entry.module)
    except ModuleNotFoundError as error:
        if error.name == entry.module or entry.module.startswith(f"{error.name}."):
            raise MalformedApp(
                f"entry point {entry.value!r} points at a module that isn't installed: {error}"
            ) from error
        # A dependency the entry module imports is missing — its code ran and failed.
        raise AppImportError(
            f"app entry module {entry.module!r} failed to import: {error}"
        ) from error
    except Exception as error:
        raise AppImportError(
            f"app entry module {entry.module!r} failed to import: {error}"
        ) from error

    app = module
    for attribute in filter(None, (entry.attr or "").split(".")):
        try:
            app = getattr(app, attribute)
        except AttributeError as error:
            raise MalformedApp(
                f"entry point {entry.value!r} names {attribute!r}, which its module doesn't define"
            ) from error
    if not (isinstance(app, type) and issubclass(app, App)):
        raise MalformedApp(f"entry point {entry.value!r} is not an App")
    return app


def _tables_declared_in(package: str) -> set[str]:
    # A table belongs to whoever declared its model, not to whoever happened to import
    # it first: an app entry module pulls its own models in, and a sibling's.
    from druks.models import Base

    return {
        mapper.local_table.name
        for mapper in Base.registry.mappers
        if mapper.class_.__module__.startswith(f"{package}.") and mapper.local_table is not None
    }


def import_app_models(only: type[App] | None = None) -> None:
    """Import apps' ``<package>.models`` so their tables register on the shared
    metadata before ``create_all`` or autogenerate. Defaults to every installed
    app (boot, migrations); pass ``only`` to register a single app's tables
    for a headless load. A separately-shipped app's tables must carry its
    ``<name>_`` prefix — the platform scopes its migrations by that prefix, so an
    unprefixed table would be invisible to them and fails the load instead. Builtin
    apps are exempt: their schema is core's."""
    import importlib
    import importlib.util

    apps = [only] if only else iter_apps()
    for app in apps:
        name = f"{app.package}.models"
        if not importlib.util.find_spec(name):
            continue
        importlib.import_module(name)
        misnamed = sorted(
            table
            for table in _tables_declared_in(app.package)
            if not table.startswith(app.table_prefix)
        )
        if misnamed and app.prefix_tables and not app.builtin:
            raise ValueError(
                f"app {app.name!r} tables must start with {app.table_prefix!r}: {misnamed}"
            )


def mount(api: "FastAPI", app: type[App], modules: list["ModuleType"]) -> None:
    """Mount one discovered app under ``/api/<name>`` and ``/app/<name>``.
    The prefix and the identity gate are the loader's — no app hook can
    override them."""
    # Local, matching get_routers: the loader stays importable headlessly.
    from fastapi import Depends

    from druks.accounts.dependencies import current_account

    prefix = f"/api/{app.name}"
    for router in app.get_routers(modules):
        api.include_router(
            router,
            prefix=prefix,
            tags=[app.name],
            dependencies=[Depends(current_account)],
        )
    dist = app.frontend_dist()
    if dist:
        # /app, not /api: unknown /api/* paths stay JSON 404s.
        api.frontend(f"/app/{app.name}", directory=dist)


def load(api: "FastAPI") -> None:
    """API boot: for each app — discover, validate subjects, mount."""
    # The table-prefix check runs here, not just in makemigrations — an author
    # who hand-writes migrations still can't boot with an unprefixed table.
    import_app_models()
    for app in iter_apps():
        modules = app.discover()
        app.subjects()
        mount(api, app, modules)

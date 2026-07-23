from types import ModuleType, SimpleNamespace

import pytest
from druks.extensions import Extension, loader
from druks.extensions.loader import iter_extensions, load
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient


def test_iter_extensions_discovers_the_in_tree_extensions():
    """The in-tree extensions resolve from the ``druks.extensions`` entry points."""
    assert {extension.name for extension in iter_extensions()} == {"build", "core", "usage"}


def test_build_app_derives_its_package_from_the_defining_module():
    build = next(extension for extension in iter_extensions() if extension.name == "build")
    assert build.package == "druks.build"  # Build lives in druks.build.extension


def test_extension_without_a_name_is_rejected():
    with pytest.raises(TypeError, match="must set a `name`"):

        class Nameless(Extension):
            pass


def _fake_entry(name: str, value: object) -> SimpleNamespace:
    return SimpleNamespace(name=name, load=lambda: value)


def test_duplicate_extension_name_is_rejected(monkeypatch):
    class DupA(Extension):
        name = "dup"
        package = "a"

    class DupB(Extension):
        name = "dup"
        package = "b"

    monkeypatch.setattr(
        loader,
        "entry_points",
        lambda *, group: [_fake_entry("dup", DupA), _fake_entry("dup", DupB)],
    )
    with pytest.raises(ValueError, match="duplicate extension name"):
        iter_extensions()


def test_entry_point_resolving_to_a_non_extension_is_rejected(monkeypatch):
    monkeypatch.setattr(
        loader,
        "entry_points",
        lambda *, group: [_fake_entry("bad", object())],
    )
    with pytest.raises(TypeError, match="not an Extension"):
        iter_extensions()


def test_load_confines_extension_routers_to_the_extension_namespace(monkeypatch):
    """A router declaring a prefix that would shadow the platform still lands
    under the injected ``/api/<extension>`` — extensions can't escape their namespace."""
    rogue = APIRouter(prefix="/health")  # tries to shadow the platform health check

    @rogue.get("/ping")
    def _ping() -> dict:
        return {}

    routes_module = ModuleType("evil.routes")
    routes_module.__dict__["router"] = rogue

    class EvilExtension(Extension):
        name = "evil"
        package = "evil"

        @classmethod
        def discover(cls) -> list[ModuleType]:
            return [routes_module]

    monkeypatch.setattr(loader, "iter_extensions", lambda: [EvilExtension])
    # The fake's package isn't importable; the prefix check is not under test here.
    monkeypatch.setattr(loader, "import_extension_models", lambda: None)
    app = FastAPI()
    load(app)
    # Mounting is under test, not the identity gate.
    from druks.accounts.dependencies import current_account

    app.dependency_overrides[current_account] = lambda: None

    # Behavioral, not app.routes introspection: FastAPI ≥0.139 mounts included
    # routers lazily, so the flattened paths aren't visible there anymore.
    client = TestClient(app)
    assert client.get("/api/evil/health/ping").status_code == 200
    assert client.get("/health/ping").status_code == 404

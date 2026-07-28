from types import ModuleType, SimpleNamespace

import pytest
from druks.extensions import Extension, loader
from druks.extensions.loader import iter_extensions, load
from druks.workflows import Subject
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient


class Widget(Subject):
    """What the fake extensions below are about. A workflow is what declares a subject
    in a real extension; these stand in for that."""

    @classmethod
    def list_summaries(cls) -> list:
        return []


def _subject_classes(cls) -> list[type[Subject]]:
    return [Widget]


def test_iter_extensions_discovers_the_bundled_extensions():
    """The bundled extensions resolve from the ``druks.extensions`` entry points."""
    assert {extension.name for extension in iter_extensions()} >= {"core", "ship", "usage"}


def test_ship_app_derives_its_package_from_the_defining_module():
    ship = next(extension for extension in iter_extensions() if extension.name == "ship")
    assert ship.package == "druks.contrib.ship"


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


def _routes_module(name: str, **routers: APIRouter) -> ModuleType:
    module = ModuleType(f"{name}.routes")
    module.__dict__.update(routers)
    return module


def _mount(extension: type[Extension], monkeypatch) -> TestClient:
    monkeypatch.setattr(loader, "iter_extensions", lambda: [extension])
    monkeypatch.setattr(loader, "import_extension_models", lambda: None)
    app = FastAPI()
    load(app)
    from druks.accounts.dependencies import current_account

    app.dependency_overrides[current_account] = lambda: None
    return TestClient(app)


def test_nothing_an_extension_declares_can_take_a_read_the_platform_serves(monkeypatch):
    """Not even a catch-all: the platform's two segments are matched before any router
    an extension declares, so an author never has to know they are reserved."""
    greedy = APIRouter()

    @greedy.get("/{anything:path}")
    def _greedy(anything: str) -> dict:
        return {"who": "extension"}

    class Greedy(Extension):
        name = "greedy"
        package = "greedy"
        subject_classes = classmethod(_subject_classes)

        @classmethod
        def discover(cls) -> list[ModuleType]:
            return [_routes_module("greedy", router=greedy)]

    client = _mount(Greedy, monkeypatch)
    assert client.get("/api/greedy/widget").json() == {"rows": []}
    assert client.get("/api/greedy/anything-else").json() == {"who": "extension"}


def test_a_composed_router_loads(monkeypatch):
    """``parent.include_router(child)`` leaves a lazily-flattened entry behind, and an
    extension that composes its routes is an ordinary one."""
    parent, child = APIRouter(prefix="/parts"), APIRouter(prefix="/nested")

    @child.get("")
    def _nested() -> dict:
        return {"who": "nested"}

    parent.include_router(child)

    class Composed(Extension):
        name = "composed"
        package = "composed"
        subject_classes = classmethod(_subject_classes)

        @classmethod
        def discover(cls) -> list[ModuleType]:
            return [_routes_module("composed", router=parent)]

    assert _mount(Composed, monkeypatch).get("/api/composed/parts/nested").json() == {
        "who": "nested"
    }


def test_a_subject_cannot_take_the_transcripts_segment():
    """Every extension's agent-call reads live there, so the collision is between two
    platform surfaces — an author would never see which one answered."""

    class Transcripts(Subject):
        pass

    class Colliding(Extension):
        name = "colliding"
        package = "colliding"

        @classmethod
        def workflows(cls):
            return [SimpleNamespace(subject=Transcripts)]

    with pytest.raises(TypeError, match="agent-call reads"):
        Colliding.subject_classes()


def test_an_extension_declaring_a_subject_type_is_rejected():
    """The workflow says what its runs are about; an extension repeating it would be a
    second spelling that can go stale."""
    with pytest.raises(TypeError, match="declares subject_type"):

        class Stale(Extension):
            name = "stale"
            subject_type = "widget"


def test_an_extensions_subjects_come_from_its_workflows():
    ship = next(extension for extension in iter_extensions() if extension.name == "ship")
    ship.discover()

    assert [s.subject_type for s in ship.subject_classes()] == ["project_repo", "work_item"]


def test_the_extension_name_tags_every_route(monkeypatch):
    """An author writes what their router serves; the platform says whose it is."""
    router = APIRouter(prefix="/parts")

    @router.get("")
    def _parts() -> dict:
        return {}

    class Tagged(Extension):
        name = "tagged"
        package = "tagged"

        @classmethod
        def discover(cls) -> list[ModuleType]:
            return [_routes_module("tagged", router=router)]

    monkeypatch.setattr(loader, "iter_extensions", lambda: [Tagged])
    monkeypatch.setattr(loader, "import_extension_models", lambda: None)
    app = FastAPI()
    load(app)
    assert app.openapi()["paths"]["/api/tagged/parts"]["get"]["tags"] == ["tagged"]

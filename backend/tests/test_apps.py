from types import ModuleType, SimpleNamespace

import pytest
from druks.apps import App, loader
from druks.apps.exceptions import AppSubjectContractError
from druks.apps.loader import iter_apps, load
from druks.workflows import Subject
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient


class Widget(Subject):
    """What the fake apps below are about. A workflow is what declares a subject
    in a real app; these stand in for that."""

    @classmethod
    async def list_summaries(cls, account_id: str | None) -> list:
        return []


def _subjects(cls) -> list[type[Subject]]:
    return [Widget]


def test_iter_apps_discovers_the_bundled_apps():
    """The bundled apps resolve from the ``druks.apps`` entry points."""
    assert {app.name for app in iter_apps()} >= {"core", "ship", "usage"}


def test_platform_apps_are_builtin():
    """``builtin`` is what keeps a platform surface out of the shell's app switcher."""
    builtin = {app.name for app in iter_apps() if app.builtin}
    assert builtin >= {"core", "usage"}


def test_ship_app_derives_its_package_from_the_defining_module():
    ship = next(app for app in iter_apps() if app.name == "ship")
    assert ship.package == "druks.contrib.ship"


def test_app_without_a_name_is_rejected():
    with pytest.raises(TypeError, match="must set a `name`"):

        class Nameless(App):
            pass


def _fake_entry(name: str, value: object) -> SimpleNamespace:
    return SimpleNamespace(name=name, load=lambda: value)


def test_duplicate_app_name_is_rejected(monkeypatch):
    class DupA(App):
        name = "dup"
        package = "a"

    class DupB(App):
        name = "dup"
        package = "b"

    monkeypatch.setattr(
        loader,
        "entry_points",
        lambda *, group: [_fake_entry("dup", DupA), _fake_entry("dup", DupB)],
    )
    with pytest.raises(ValueError, match="duplicate app name"):
        iter_apps()


def test_entry_point_resolving_to_a_non_app_is_rejected(monkeypatch):
    monkeypatch.setattr(
        loader,
        "entry_points",
        lambda *, group: [_fake_entry("bad", object())],
    )
    with pytest.raises(TypeError, match="not an App"):
        iter_apps()


def test_load_confines_app_routers_to_the_app_namespace(monkeypatch):
    """A router declaring a prefix that would shadow the platform still lands
    under the injected ``/api/<app>`` — apps can't escape their namespace."""
    rogue = APIRouter(prefix="/health")  # tries to shadow the platform health check

    @rogue.get("/ping")
    def _ping() -> dict:
        return {}

    routes_module = ModuleType("evil.routes")
    routes_module.__dict__["router"] = rogue

    class EvilApp(App):
        name = "evil"
        package = "evil"

        @classmethod
        def discover(cls) -> list[ModuleType]:
            return [routes_module]

    monkeypatch.setattr(loader, "iter_apps", lambda: [EvilApp])
    # The fake's package isn't importable; the prefix check is not under test here.
    monkeypatch.setattr(loader, "import_app_models", lambda: None)
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


def _boot(registered_app: type[App], monkeypatch) -> TestClient:
    monkeypatch.setattr(loader, "iter_apps", lambda: [registered_app])
    monkeypatch.setattr(loader, "import_app_models", lambda: None)
    api = FastAPI()
    load(api)
    from druks.accounts.dependencies import current_account

    api.dependency_overrides[current_account] = lambda: None
    return TestClient(api)


def test_nothing_an_app_declares_can_take_a_read_the_platform_serves(monkeypatch):
    """Not even a catch-all: the platform's two segments are matched before any router
    an app declares, so an author never has to know they are reserved."""
    greedy = APIRouter()

    @greedy.get("/{anything:path}")
    def _greedy(anything: str) -> dict:
        return {"who": "app"}

    class Greedy(App):
        name = "greedy"
        package = "greedy"
        subjects = classmethod(_subjects)

        @classmethod
        def discover(cls) -> list[ModuleType]:
            return [_routes_module("greedy", router=greedy)]

    client = _boot(Greedy, monkeypatch)
    assert client.get("/api/greedy/widget").json() == {"rows": []}
    assert client.get("/api/greedy/anything-else").json() == {"who": "app"}


def test_a_composed_router_loads(monkeypatch):
    """``parent.include_router(child)`` leaves a lazily-flattened entry behind, and an
    app that composes its routes is an ordinary one."""
    parent, child = APIRouter(prefix="/parts"), APIRouter(prefix="/nested")

    @child.get("")
    def _nested() -> dict:
        return {"who": "nested"}

    parent.include_router(child)

    class Composed(App):
        name = "composed"
        package = "composed"
        subjects = classmethod(_subjects)

        @classmethod
        def discover(cls) -> list[ModuleType]:
            return [_routes_module("composed", router=parent)]

    assert _boot(Composed, monkeypatch).get("/api/composed/parts/nested").json() == {
        "who": "nested"
    }


def test_a_subject_cannot_take_the_transcripts_segment():
    """Every app's agent-call reads live there, so the collision is between two
    platform surfaces — an author would never see which one answered."""

    class Transcripts(Subject):
        pass

    class Colliding(App):
        name = "colliding"
        package = "colliding"

        @classmethod
        def workflows(cls):
            return [SimpleNamespace(subject=Transcripts)]

    with pytest.raises(AppSubjectContractError, match="agent-call reads"):
        Colliding.subjects()


def test_a_declared_subject_missing_list_summaries_is_rejected_with_an_actionable_error():
    class Account(Subject):
        pass  # inherits the platform stub

    class Broken(App):
        name = "broken"
        package = "broken"

        @classmethod
        def workflows(cls):
            return [SimpleNamespace(subject=Account)]

    with pytest.raises(AppSubjectContractError) as caught:
        Broken.subjects()
    message = str(caught.value)
    assert "broken" in message  # the app name
    assert "Account" in message  # the subject class
    assert "list_summaries()" in message  # the missing method
    assert "Implement list_summaries()" in message  # the implementation direction


def test_full_boot_refuses_a_subject_missing_list_summaries(monkeypatch):
    """An app with nothing to mount still fails — the gate is a loader stage."""

    class Account(Subject):
        pass  # inherits the platform stub

    class Broken(App):
        name = "broken_boot"
        package = "broken_boot"

        @classmethod
        def discover(cls) -> list[ModuleType]:
            return []

        @classmethod
        def workflows(cls):
            return [SimpleNamespace(subject=Account)]

    monkeypatch.setattr(loader, "iter_apps", lambda: [Broken])
    monkeypatch.setattr(loader, "import_app_models", lambda: None)
    with pytest.raises(AppSubjectContractError, match="list_summaries"):
        load(FastAPI())


def test_a_concrete_inherited_list_summaries_satisfies_the_contract_without_calling_it():
    class Concrete(Subject):
        @classmethod
        def list_summaries(cls, account_id: str | None) -> list:
            raise AssertionError("validation must not call list_summaries()")

    class Inheritor(Concrete):
        pass

    class Fine(App):
        name = "fine"
        package = "fine"

        @classmethod
        def workflows(cls):
            return [SimpleNamespace(subject=Inheritor)]

    assert Fine.subjects() == [Inheritor]


def test_an_app_declaring_a_subject_type_is_rejected():
    """The workflow says what its runs are about; an app repeating it would be a
    second spelling that can go stale."""
    with pytest.raises(TypeError, match="declares subject_type"):

        class Stale(App):
            name = "stale"
            subject_type = "widget"


def test_an_apps_subjects_come_from_its_workflows():
    ship = next(app for app in iter_apps() if app.name == "ship")
    ship.discover()

    assert [subject.subject_type for subject in ship.subjects()] == ["project_repo", "work_item"]


def test_the_app_name_tags_every_route(monkeypatch):
    """An author writes what their router serves; the platform says whose it is."""
    router = APIRouter(prefix="/parts")

    @router.get("")
    def _parts() -> dict:
        return {}

    class Tagged(App):
        name = "tagged"
        package = "tagged"

        @classmethod
        def discover(cls) -> list[ModuleType]:
            return [_routes_module("tagged", router=router)]

    monkeypatch.setattr(loader, "iter_apps", lambda: [Tagged])
    monkeypatch.setattr(loader, "import_app_models", lambda: None)
    app = FastAPI()
    load(app)
    assert app.openapi()["paths"]["/api/tagged/parts"]["get"]["tags"] == ["tagged"]

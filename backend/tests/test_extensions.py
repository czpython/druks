from types import ModuleType, SimpleNamespace

import pytest
from druks.extensions import Extension, loader
from druks.extensions.exceptions import RouteDeclarationError
from druks.extensions.loader import iter_extensions, load
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient


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


def test_declared_routers_mount_ahead_of_the_platforms():
    """Order is the contract: the author's routers are matched first, so the platform's
    own reads are the fallback, never the shadow."""

    class Ordered(Extension):
        name = "ordered"
        package = "ordered"
        subject_type = "widget"

    module = _routes_module("ordered", router=APIRouter(prefix="/parts"))
    paths = [router.prefix for router in Ordered.get_routers([module])]
    assert paths == ["/parts", "/transcripts/{call_id}", "/widget"]


@pytest.mark.parametrize("prefix", ["/widget", "/widget/settings"])
def test_a_router_reaching_into_the_subject_namespace_is_rejected(prefix: str):
    """Declared routers mount first, so this one would take the board or a detail read
    with it — and nothing collides at import, so it has to fail here."""

    class Claiming(Extension):
        name = "claiming"
        package = "claiming"
        subject_type = "widget"

    claimed = APIRouter(prefix=prefix)

    @claimed.get("")
    def _read() -> dict:
        return {}

    with pytest.raises(RouteDeclarationError, match="subject read-side"):
        Claiming.get_routers([_routes_module("claiming", router=claimed)])


def test_a_router_named_like_the_subject_is_left_alone():
    """The rule is the segment, not the spelling — ``/widgets`` is an author's own
    resource and says nothing about the platform's ``/widget``."""

    class Neighbour(Extension):
        name = "neighbour"
        package = "neighbour"
        subject_type = "widget"

    Neighbour.get_routers([_routes_module("neighbour", router=APIRouter(prefix="/widgets"))])


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

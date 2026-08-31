import importlib
import sys

import pytest
from druks.apps.loader import _workflow_packages, mount, register_workflow_package
from druks.scaffolding import create_app
from fastapi import FastAPI
from fastapi.testclient import TestClient


def test_create_app_scaffolds_a_loadable_package(tmp_path):
    target = create_app("night_watch", tmp_path)

    assert target == tmp_path / "druks-night_watch"
    package = target / "druks_night_watch"
    assert (package / "migrations" / "versions").is_dir()
    rendered = [path for path in target.rglob("*") if path.is_file()]
    assert rendered
    # The generated suite is what an author runs first; the scaffold is useless without it.
    assert (target / "tests" / "test_app.py").is_file()
    # Screens are Python. The scaffold writes no JavaScript and no dist/.
    assert (package / "pages.py").is_file()
    assert not (package / "dist").exists()
    assert 'navigation = ["overview"]' in (package / "app.py").read_text()
    for path in rendered:
        assert "-tpl" not in path.name
        assert "{{" not in path.read_text()
    assert (
        'night_watch = "druks_night_watch.app:NightWatch"'
        in (target / "pyproject.toml").read_text()
    )

    # No rendered file may reference retired surfaces: the old storage namespace, the
    # taskiq worker, the one-arg ``config`` workflow wording, or either spelling of a
    # subject type an author no longer writes — the workflow's declaration is the one.
    for path in rendered:
        text = path.read_text()
        lowered = text.lower()
        assert "druks.storage" not in text
        assert "taskiq" not in lowered
        assert not path.name.endswith(".js")
        assert "shellApi" not in text
        assert "``config``" not in text
        assert "subject_type" not in text
        assert "Subject(" not in text

    # The generated app.py must survive App.__init_subclass__ validation,
    # and mounting must serve its API routes and the pages it declares.
    sys.path.insert(0, str(target))
    try:
        module = importlib.import_module("druks_night_watch.app")
        night_watch = module.NightWatch
        assert night_watch.name == "night_watch"
        assert night_watch.table_prefix == "night_watch_"
        assert night_watch.package == "druks_night_watch"

        # An installed app has its package claimed by the loader before any
        # module imports; the generated workflow resolves its identity from that.
        register_workflow_package(night_watch.package, night_watch.name)

        for role in (
            "models",
            "schemas",
            "contracts",
            "workflows",
            "routes",
            "pages",
            "subscribers",
        ):
            importlib.import_module(f"druks_night_watch.{role}")

        # The workflow guidance must not teach a per-run app= argument —
        # a workflow's identity comes from its declaring app.
        assert "app=" not in (target / "druks_night_watch" / "workflows.py").read_text()

        api = FastAPI()
        mount(api, night_watch, night_watch.discover())
        # Scaffolding is under test, not the identity gate.
        from druks.accounts.dependencies import current_account

        api.dependency_overrides[current_account] = lambda: None
        client = TestClient(api)
        assert client.get("/api/night_watch/status").json() == {"app": "night_watch"}
        # The landing page is the scaffold's own, and it renders with no
        # JavaScript anywhere in the package.
        landing = client.get("/api/night_watch/pages")
        assert landing.status_code == 200
        assert landing.json()["title"] == "NightWatch"
        assert landing.json()["blocks"][0]["block"] == "stack"
        assert night_watch.frontend_dist() is None
        assert [page.name for page in night_watch.pages()] == ["overview"]
        assert [page.label for page in night_watch.navigation_pages()] == ["overview"]
    finally:
        sys.path.remove(str(target))
        _workflow_packages.pop("druks_night_watch", None)
        for name in [m for m in sys.modules if m.startswith("druks_night_watch")]:
            del sys.modules[name]


def test_create_app_rejects_bad_and_taken_names(tmp_path):
    with pytest.raises(ValueError, match="must match"):
        create_app("Night-Watch", tmp_path)
    with pytest.raises(ValueError, match="already installed"):
        create_app("software_factory", tmp_path)
    create_app("night_watch", tmp_path)
    with pytest.raises(ValueError, match="already exists"):
        create_app("night_watch", tmp_path)

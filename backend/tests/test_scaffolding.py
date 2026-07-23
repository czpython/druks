import importlib
import sys

import pytest
from druks.scaffolding import create_extension
from fastapi import FastAPI
from fastapi.testclient import TestClient


def test_create_extension_scaffolds_a_loadable_package(tmp_path):
    target = create_extension("night_watch", tmp_path)

    assert target == tmp_path / "druks-night_watch"
    package = target / "druks_night_watch"
    assert (package / "migrations" / "versions").is_dir()
    rendered = [path for path in target.rglob("*") if path.is_file()]
    assert rendered
    for path in rendered:
        assert "-tpl" not in path.name
        assert "{{" not in path.read_text()
    assert (
        'night_watch = "druks_night_watch.extension:NightWatch"'
        in (target / "pyproject.toml").read_text()
    )

    # No rendered file may reference retired surfaces: the old storage namespace, the
    # taskiq worker, or the one-arg ``config`` workflow wording.
    for path in rendered:
        text = path.read_text()
        lowered = text.lower()
        assert "druks.storage" not in text
        assert "taskiq" not in lowered
        assert "``config``" not in text

    # The generated extension.py must survive Extension.__init_subclass__ validation,
    # and load() must mount both the API routes and the shipped dist/ frontend.
    sys.path.insert(0, str(target))
    try:
        module = importlib.import_module("druks_night_watch.extension")
        extension = module.NightWatch
        assert extension.name == "night_watch"
        assert extension.table_prefix == "night_watch_"
        assert extension.package == "druks_night_watch"

        for role in ("models", "schemas", "contracts", "workflows", "routes", "subscribers"):
            importlib.import_module(f"druks_night_watch.{role}")

        # The workflow guidance must not teach a per-run extension= argument —
        # a workflow's identity comes from its declaring extension.
        assert "extension=" not in (target / "druks_night_watch" / "workflows.py").read_text()

        app = FastAPI()
        extension.load(app)
        # Scaffolding is under test, not the identity gate.
        from druks.accounts.dependencies import current_account

        app.dependency_overrides[current_account] = lambda: None
        client = TestClient(app)
        assert client.get("/api/night_watch/status").json() == {"extension": "night_watch"}
        page = client.get("/app/night_watch/")
        assert page.status_code == 200
        assert "night_watch" in page.text
    finally:
        sys.path.remove(str(target))
        for name in [m for m in sys.modules if m.startswith("druks_night_watch")]:
            del sys.modules[name]


def test_create_extension_rejects_bad_and_taken_names(tmp_path):
    with pytest.raises(ValueError, match="must match"):
        create_extension("Night-Watch", tmp_path)
    with pytest.raises(ValueError, match="already installed"):
        create_extension("build", tmp_path)
    create_extension("night_watch", tmp_path)
    with pytest.raises(ValueError, match="already exists"):
        create_extension("night_watch", tmp_path)

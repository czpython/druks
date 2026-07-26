from pathlib import Path

from druks.api.app import SpaCacheControl, _release_db_session, serve_spa
from druks.database import db_session
from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient


def _app_with_spa(dist: Path) -> FastAPI:
    (dist / "index.html").write_text("<html>druks</html>")
    (dist / "assets").mkdir()
    (dist / "assets" / "app.js").write_text("console.log(1)")
    app = FastAPI()

    @app.get("/api/thing")
    async def thing() -> dict[str, str]:
        return {"ok": "yes"}

    @app.api_route("/api/{path:path}", methods=["GET"], include_in_schema=False)
    async def api_not_found(path: str) -> None:
        raise HTTPException(status_code=404)

    serve_spa(app, dist)
    return app


def test_spa_serves_index_assets_and_client_routes(tmp_path):
    client = TestClient(_app_with_spa(tmp_path))
    assert "druks" in client.get("/").text
    assert client.get("/assets/app.js").status_code == 200
    # An unknown path is a client-side route: index.html, not a 404.
    assert "druks" in client.get("/work_items/42").text


def test_api_paths_win_over_the_spa(tmp_path):
    client = TestClient(_app_with_spa(tmp_path))
    assert client.get("/api/thing").json() == {"ok": "yes"}
    # Unknown API paths stay JSON 404s, never fall through to index.html.
    assert client.get("/api/nope").status_code == 404


def test_no_spa_build_serves_nothing(tmp_path):
    app = FastAPI()
    serve_spa(app, tmp_path)  # no index.html here
    assert TestClient(app).get("/").status_code == 404


def test_cache_policy_lives_with_the_server(tmp_path):
    app = _app_with_spa(tmp_path)
    app.add_middleware(SpaCacheControl)
    client = TestClient(app)
    # Fingerprinted assets never change for a given URL — cache forever.
    asset = client.get("/assets/app.js")
    assert asset.headers["cache-control"] == "public, max-age=31536000, immutable"
    # index.html must revalidate every load or a deploy leaves stale bundles.
    assert client.get("/").headers["cache-control"] == "no-cache"
    assert client.get("/work_items/42").headers["cache-control"] == "no-cache"
    # API responses carry no frontend cache policy; nor does a missing asset.
    assert "cache-control" not in client.get("/api/thing").headers
    assert "cache-control" not in client.get("/assets/gone.js").headers


def test_request_that_never_touches_the_db_opens_no_session(monkeypatch):
    opened = []
    original = db_session.registry.createfunc

    def counting():
        opened.append(1)
        return original()

    monkeypatch.setattr(db_session.registry, "createfunc", counting)
    # The harness binds an ambient session; drop it so what the registry holds here
    # is only what a request opened.
    db_session.remove()
    app = FastAPI(dependencies=[Depends(_release_db_session)])

    @app.get("/plain")
    async def plain() -> dict[str, str]:
        return {}

    @app.get("/touch")
    async def touch() -> dict[str, str]:
        db_session()
        return {}

    client = TestClient(app)
    # The SPA/asset case: the boundary must not open a session to commit nothing.
    assert client.get("/plain").status_code == 200
    assert not opened
    # The API case: a session the request opened still gets committed + released.
    assert client.get("/touch").status_code == 200
    assert opened == [1]
    assert not db_session.registry.has()

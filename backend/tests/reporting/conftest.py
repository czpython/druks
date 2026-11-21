"""Fixtures for the measurement scripts in scripts/measure/.

Each test runs a script as its own entry point, against synthetic files and a fake
dashboard. The tests need no database, no Redis, and no network.
"""

import io
import json
import runpy
import shutil
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

MEASURE = Path(__file__).resolve().parents[3] / "scripts" / "measure"


class Dashboard:
    """A fake dashboard API. It answers from a table of URLs and keeps every request."""

    base = "https://dashboard.test"
    cookie = "druks_session=s3cr3t-session-value"

    def __init__(self):
        self.responses = {}
        self.requests = []

    @property
    def paths(self):
        return [request.full_url.removeprefix(self.base) for request in self.requests]

    def serve(self, path, content):
        self.responses[self.base + path] = content

    def serve_json(self, path, value):
        self.serve(path, json.dumps(value).encode())

    def urlopen(self, request, timeout):
        self.requests.append(request)
        if request.full_url in self.responses:
            return io.BytesIO(self.responses[request.full_url])
        raise urllib.error.HTTPError(request.full_url, 404, "Not Found", {}, None)


@pytest.fixture
def dashboard(monkeypatch):
    dashboard = Dashboard()
    monkeypatch.setattr(urllib.request, "urlopen", dashboard.urlopen)
    return dashboard


@pytest.fixture
def cookie_file(tmp_path, dashboard):
    path = tmp_path / "cookie"
    path.write_text(f"Cookie: {dashboard.cookie}\n")
    return path


@pytest.fixture
def checkout(tmp_path):
    """A throwaway repository with a copy of scripts/measure/. Some scripts write beside
    themselves or under the repository, so every test runs the copy."""
    shutil.copytree(
        MEASURE,
        tmp_path / "checkout" / "scripts" / "measure",
        ignore=shutil.ignore_patterns("__pycache__", "*.json"),
    )
    return tmp_path / "checkout"


@pytest.fixture
def run_script(monkeypatch, checkout):
    """Run a script of the checkout as ``python3 <script> <arguments>`` runs it."""
    monkeypatch.setattr(sys, "path", [*sys.path])

    def run(name, *arguments):
        script = checkout / "scripts" / "measure" / name
        monkeypatch.setattr(sys, "argv", [str(script), *map(str, arguments)])
        runpy.run_path(str(script), run_name="__main__")

    yield run
    # A script imports its siblings by file name. The next test imports its own copies.
    for name in ("fetch-event-transcripts", "find-long-runs"):
        sys.modules.pop(name, None)

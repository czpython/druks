from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path: Path, db_session, monkeypatch):
    from conftest import configure_app_for_test, make_settings

    monkeypatch.setenv("DRUKS_DATA_DIR", str(tmp_path))
    app = configure_app_for_test(settings=make_settings(tmp_path))
    with TestClient(app) as client:
        yield client


def _stub_profile_start(monkeypatch):
    """Profile.start hits DBOS's queue — route tests only need to prove the
    route calls it with the right subject/input, not run it."""
    from druks.build.workflows import Profile

    calls: list[dict] = []

    async def _start(cls, *, subject, **input):
        calls.append({"subject": subject, **input})
        return "fake-run-id"

    monkeypatch.setattr(Profile, "start", classmethod(_start))
    return calls


def test_adding_a_repo_dispatches_a_profile_run(client: TestClient, monkeypatch):
    calls = _stub_profile_start(monkeypatch)

    project = client.post("/api/build/projects", json={"name": "Acme"}).json()
    repo = client.post(
        f"/api/build/projects/{project['id']}/repos",
        json={"fullName": "acme/widget"},
    ).json()

    assert calls == [
        {
            "subject": {"type": "project_repo", "id": repo["id"]},
            "repo_id": repo["id"],
        }
    ]
    assert repo["profileStatus"] == "unprofiled"


def test_profile_endpoint_dispatches(client: TestClient, monkeypatch):
    # Concurrency is the Profile workflow's subject-unique lock, not the route's
    # job — the route always dispatches and start() dedups against a live run.
    from druks.build.models import Project, ProjectRepo

    calls = _stub_profile_start(monkeypatch)
    project = Project.create(name="Acme")
    repo = ProjectRepo.create(project_id=project.id, full_name="acme/widget")

    response = client.post(f"/api/build/projects/{project.id}/repos/{repo.id}/profile")

    assert response.status_code == 200
    assert calls == [
        {
            "subject": {"type": "project_repo", "id": repo.id},
            "repo_id": repo.id,
        }
    ]


def test_nested_repo_routes_are_scoped_to_their_project(client: TestClient, monkeypatch):
    """PATCH / profile / DELETE reached through the wrong project's URL are 404 and
    side-effect-free — the routes scope by (project_id, repo_id), not repo_id alone."""
    from druks.build.models import Project, ProjectRepo

    profile_calls = _stub_profile_start(monkeypatch)
    owner = Project.create(name="Owner")
    other = Project.create(name="Other")
    repo_id = ProjectRepo.create(project_id=owner.id, full_name="acme/widget").id

    wrong = f"/api/build/projects/{other.id}/repos/{repo_id}"
    assert client.patch(wrong, json={"purpose": "infra"}).status_code == 404
    assert client.post(f"{wrong}/profile").status_code == 404
    assert client.delete(wrong).status_code == 404
    # None of the wrong-parent calls mutated the repo or dispatched a profile run.
    assert ProjectRepo.get(repo_id).purpose is None
    assert profile_calls == []

    # Through its own project the repo mutates and deletes as normal.
    right = f"/api/build/projects/{owner.id}/repos/{repo_id}"
    patched = client.patch(right, json={"purpose": "infra"})
    assert patched.status_code == 200
    assert patched.json()["purpose"] == "infra"
    assert client.delete(right).status_code == 204
    assert ProjectRepo.get(repo_id) is None

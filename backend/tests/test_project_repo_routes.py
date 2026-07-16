from pathlib import Path

import pytest
from druks.accounts.models import Account
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

    async def _start(cls, *, subject, account_id=None, **input):
        calls.append({"subject": subject, "account_id": account_id, **input})
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

    account = Account.get_for_email("op@example.com")
    assert calls == [
        {
            "subject": {"type": "project_repo", "id": repo["id"]},
            "account_id": account.id,
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
    account = Account.get_for_email("op@example.com")
    assert calls == [
        {
            "subject": {"type": "project_repo", "id": repo.id},
            "account_id": account.id,
            "repo_id": repo.id,
        }
    ]

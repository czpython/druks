from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path: Path, druks_db, monkeypatch):
    from druks.testing import configure_app_for_test, make_settings

    monkeypatch.setenv("DRUKS_DATA_DIR", str(tmp_path))
    app = configure_app_for_test(settings=make_settings(tmp_path))
    with TestClient(app) as client:
        yield client


def _stub_profile_dispatch(monkeypatch):
    """Profile.dispatch hits DBOS's queue — route tests only prove they hand it
    the registered repo."""
    from druks.contrib.ship.workflows import Profile

    calls: list[dict] = []

    async def _dispatch(cls, repo, *, refresh_only=False):
        calls.append({"repo_id": repo.id, "refresh_only": refresh_only})
        return "fake-run-id"

    monkeypatch.setattr(Profile, "dispatch", classmethod(_dispatch))
    return calls


def test_adding_a_repo_dispatches_a_profile_run(client: TestClient, monkeypatch):
    calls = _stub_profile_dispatch(monkeypatch)

    project = client.post("/api/ship/projects", json={"name": "Acme"}).json()
    repo = client.post(
        f"/api/ship/projects/{project['id']}/repos",
        json={"fullName": "acme/widget"},
    ).json()

    assert calls == [
        {
            "repo_id": int(repo["id"]),
            "refresh_only": False,
        }
    ]
    assert repo["profile"] == {}


def test_adding_a_repo_survives_when_github_is_not_connected(client: TestClient):
    # Registering a repo is metadata; a missing GitHub identity defers profiling
    # but must not discard the repo — a rollback would 500 and lose it.
    project = client.post("/api/ship/projects", json={"name": "Acme"}).json()
    response = client.post(
        f"/api/ship/projects/{project['id']}/repos",
        json={"fullName": "acme/widget"},
    )

    assert response.status_code == 201
    assert response.json()["fullName"] == "acme/widget"


def test_profile_endpoint_dispatches(client: TestClient, monkeypatch):
    # Concurrency is the Profile workflow's subject-unique lock, not the route's
    # job — the route always dispatches and start() dedups against a live run.
    from druks.contrib.ship.models import Project, ProjectRepo

    calls = _stub_profile_dispatch(monkeypatch)
    project = Project.create(name="Acme")
    repo = ProjectRepo.create(project_id=project.id, full_name="acme/widget")

    response = client.post(f"/api/ship/projects/{project.id}/repos/{repo.id}/profile")

    assert response.status_code == 200
    assert calls == [
        {
            "repo_id": repo.id,
            "refresh_only": False,
        }
    ]


def test_nested_repo_routes_are_scoped_to_their_project(client: TestClient, monkeypatch):
    """PATCH / profile / DELETE reached through the wrong project's URL are 404 and
    side-effect-free — the routes scope by (project_id, repo_id), not repo_id alone."""
    from druks.contrib.ship.models import Project, ProjectRepo

    profile_calls = _stub_profile_dispatch(monkeypatch)
    owner = Project.create(name="Owner")
    other = Project.create(name="Other")
    repo_id = ProjectRepo.create(project_id=owner.id, full_name="acme/widget").id

    wrong = f"/api/ship/projects/{other.id}/repos/{repo_id}"
    assert client.patch(wrong, json={"purpose": "infra"}).status_code == 404
    assert client.post(f"{wrong}/profile").status_code == 404
    assert client.delete(wrong).status_code == 404
    # None of the wrong-parent calls mutated the repo or dispatched a profile run.
    assert ProjectRepo.get(repo_id).purpose is None
    assert profile_calls == []

    # Through its own project the repo mutates and deletes as normal.
    right = f"/api/ship/projects/{owner.id}/repos/{repo_id}"
    patched = client.patch(right, json={"purpose": "infra"})
    assert patched.status_code == 200
    assert patched.json()["purpose"] == "infra"
    assert client.delete(right).status_code == 204
    assert ProjectRepo.get(repo_id) is None


def _make_work_item(project_id: int, ticket_key: str, *, resolved: bool = False):
    from datetime import datetime

    from druks.contrib.ship.models import WorkItem

    item = WorkItem.create(
        project_id=project_id,
        title=ticket_key,
        ticket_key=ticket_key,
        repo="acme/widget",
    )
    if resolved:
        item.resolution = "closed"
        item.resolved_at = datetime(2026, 1, 1)
    return item


def test_deleting_a_project_cascades_its_work_items_and_spares_others(client: TestClient, druks_db):
    """DELETE cascades: the project and every work item it owns go, with no 409
    reference guard — while another project's graph is left fully intact."""
    from druks.contrib.ship.models import Project, WorkItem

    target = Project.create(name="Target")
    control = Project.create(name="Control")
    doomed = _make_work_item(target.id, "ENG-1")
    doomed_resolved = _make_work_item(target.id, "ENG-2", resolved=True)
    survivor = _make_work_item(control.id, "ENG-3")
    target_id, control_id = target.id, control.id
    doomed_id, doomed_resolved_id, survivor_id = doomed.id, doomed_resolved.id, survivor.id

    response = client.delete(f"/api/ship/projects/{target_id}")

    assert response.status_code == 204
    # The route committed on its own session; drop this session's identity map so
    # the reads below reflect the committed graph rather than cached instances.
    druks_db.expire_all()
    assert Project.get(target_id) is None
    assert WorkItem.get(doomed_id) is None
    assert WorkItem.get(doomed_resolved_id) is None
    # The control project and its work item are untouched.
    assert Project.get(control_id) is not None
    assert WorkItem.get(survivor_id) is not None


def test_the_repo_subject_read_side_mounts(client: TestClient, druks_db):
    """Profile is about a repo, so the repo gets the board and the page it never had —
    its own runs' status and timeline, keyed by the repo's id."""
    from druks.contrib.ship.models import Project, ProjectRepo
    from druks.contrib.ship.workflows import Profile
    from druks.testing import seed_run

    project = Project.create(name="Acme")
    repo = ProjectRepo.create(project_id=project.id, full_name="acme/widget")
    seed_run(druks_db, kind=Profile.kind, subject=repo, state="running")

    (row,) = client.get("/api/ship/project_repo").json()["rows"]
    assert row["summary"]["id"] == str(repo.id)
    assert row["summary"]["fullName"] == "acme/widget"
    assert row["status"]["state"] == "running"

    detail = client.get(f"/api/ship/project_repo/{repo.id}").json()
    assert detail["summary"]["fullName"] == "acme/widget"
    assert [entry["kind"] for entry in detail["timeline"]] == [Profile.kind]

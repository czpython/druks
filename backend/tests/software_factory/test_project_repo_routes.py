from pathlib import Path

import pytest
from druks.database import db_session
from fastapi.testclient import TestClient


@pytest.fixture
async def client(tmp_path: Path, druks_db, monkeypatch):
    from druks.testing import asgi_client, configure_app_for_test, make_settings

    monkeypatch.setenv("DRUKS_DATA_DIR", str(tmp_path))
    app = configure_app_for_test(settings=make_settings(tmp_path))
    async with asgi_client(app) as client:
        yield client


def _stub_profile_dispatch(monkeypatch):
    """Profile.dispatch hits DBOS's queue — route tests only prove they hand it
    the registered repo."""
    from druks.contrib.software_factory.workflows import Profile

    calls: list[dict] = []

    async def _dispatch(cls, repo, *, refresh_only=False):
        calls.append({"repo_id": repo.id, "refresh_only": refresh_only})
        return "fake-run-id"

    monkeypatch.setattr(Profile, "dispatch", classmethod(_dispatch))
    return calls


async def test_get_project_returns_the_summary_or_404(client: TestClient):
    created = (await client.post("/api/software_factory/projects", json={"name": "Acme"})).json()

    fetched = await client.get(f"/api/software_factory/projects/{created['id']}")
    assert fetched.status_code == 200
    assert fetched.json() == created
    assert (await client.get("/api/software_factory/projects/999999")).status_code == 404


async def test_adding_a_repo_dispatches_a_profile_run(client: TestClient, monkeypatch):
    calls = _stub_profile_dispatch(monkeypatch)

    project = (await client.post("/api/software_factory/projects", json={"name": "Acme"})).json()
    repo = (
        await client.post(
            f"/api/software_factory/projects/{project['id']}/repos",
            json={"fullName": "acme/widget"},
        )
    ).json()

    assert calls == [
        {
            "repo_id": int(repo["id"]),
            "refresh_only": False,
        }
    ]
    assert repo["profile"] == {}


async def test_adding_a_repo_survives_when_github_is_not_connected(client: TestClient):
    # Registering a repo is metadata; a missing GitHub identity defers profiling
    # but must not discard the repo — a rollback would 500 and lose it.
    project = (await client.post("/api/software_factory/projects", json={"name": "Acme"})).json()
    response = await client.post(
        f"/api/software_factory/projects/{project['id']}/repos",
        json={"fullName": "acme/widget"},
    )

    assert response.status_code == 201
    assert response.json()["fullName"] == "acme/widget"


async def test_profile_endpoint_dispatches(client: TestClient, monkeypatch):
    # Concurrency is the Profile workflow's subject-unique lock, not the route's
    # job — the route always dispatches and start() dedups against a live run.
    from druks.contrib.software_factory.models import Project, ProjectRepo

    calls = _stub_profile_dispatch(monkeypatch)
    project = await Project.create(name="Acme")
    repo = await ProjectRepo.create(project_id=project.id, full_name="acme/widget")

    response = await client.post(
        f"/api/software_factory/projects/{project.id}/repos/{repo.id}/profile"
    )

    assert response.status_code == 200
    assert calls == [
        {
            "repo_id": repo.id,
            "refresh_only": False,
        }
    ]


async def test_nested_repo_routes_are_scoped_to_their_project(client: TestClient, monkeypatch):
    """PATCH / profile / DELETE reached through the wrong project's URL are 404 and
    side-effect-free — the routes scope by (project_id, repo_id), not repo_id alone."""
    from druks.contrib.software_factory.models import Project, ProjectRepo

    profile_calls = _stub_profile_dispatch(monkeypatch)
    owner = await Project.create(name="Owner")
    other = await Project.create(name="Other")
    repo_id = (await ProjectRepo.create(project_id=owner.id, full_name="acme/widget")).id

    wrong = f"/api/software_factory/projects/{other.id}/repos/{repo_id}"
    assert (await client.patch(wrong, json={"purpose": "infra"})).status_code == 404
    assert (await client.post(f"{wrong}/profile")).status_code == 404
    assert (await client.delete(wrong)).status_code == 404
    # None of the wrong-parent calls mutated the repo or dispatched a profile run.
    assert (await ProjectRepo.get(repo_id)).purpose is None
    assert profile_calls == []

    # Through its own project the repo mutates and deletes as normal.
    right = f"/api/software_factory/projects/{owner.id}/repos/{repo_id}"
    patched = await client.patch(right, json={"purpose": "infra"})
    assert patched.status_code == 200
    assert patched.json()["purpose"] == "infra"
    assert (await client.delete(right)).status_code == 204
    assert await ProjectRepo.get(repo_id) is None


async def _make_work_item(project_id: int, ticket_key: str, *, resolved: bool = False):
    from datetime import datetime

    from druks.contrib.software_factory.models import WorkItem

    item = await WorkItem.create(
        project_id=project_id,
        title=ticket_key,
        ticket_key=ticket_key,
        repo="acme/widget",
    )
    if resolved:
        item.resolution = "closed"
        item.resolved_at = datetime(2026, 1, 1)
    return item


async def test_deleting_a_project_cascades_its_work_items_and_spares_others(
    client: TestClient, druks_db
):
    """DELETE cascades: the project and every work item it owns go, with no 409
    reference guard — while another project's graph is left fully intact."""
    from druks.contrib.software_factory.models import Project, WorkItem

    target = await Project.create(name="Target")
    control = await Project.create(name="Control")
    doomed = await _make_work_item(target.id, "ENG-1")
    doomed_resolved = await _make_work_item(target.id, "ENG-2", resolved=True)
    survivor = await _make_work_item(control.id, "ENG-3")
    target_id, control_id = target.id, control.id
    doomed_id, doomed_resolved_id, survivor_id = doomed.id, doomed_resolved.id, survivor.id

    response = await client.delete(f"/api/software_factory/projects/{target_id}")

    assert response.status_code == 204
    # The route committed on its own session; drop the ambient session's identity
    # map so the reads below reflect the committed graph, not cached instances.
    db_session().expunge_all()
    assert await Project.get(target_id) is None
    assert await WorkItem.get(doomed_id) is None
    assert await WorkItem.get(doomed_resolved_id) is None
    # The control project and its work item are untouched.
    assert await Project.get(control_id) is not None
    assert await WorkItem.get(survivor_id) is not None


async def test_the_repo_subject_read_side_mounts(client: TestClient, druks_db):
    """Profile is about a repo, so the repo gets the board and the page it never had —
    its own runs' status and timeline, keyed by the repo's id."""
    from druks.contrib.software_factory.models import Project, ProjectRepo
    from druks.contrib.software_factory.workflows import Profile
    from druks.testing import seed_run

    project = await Project.create(name="Acme")
    repo = await ProjectRepo.create(project_id=project.id, full_name="acme/widget")
    await seed_run(druks_db, kind=Profile.kind, subject=repo, state="running")

    (row,) = (await client.get("/api/software_factory/project_repo")).json()["rows"]
    assert row["summary"]["id"] == str(repo.id)
    assert row["summary"]["fullName"] == "acme/widget"
    assert row["status"]["state"] == "running"

    detail = (await client.get(f"/api/software_factory/project_repo/{repo.id}")).json()
    assert detail["summary"]["fullName"] == "acme/widget"
    assert [entry["kind"] for entry in detail["timeline"]] == [Profile.kind]

from types import SimpleNamespace

import druks.build.subscribers  # noqa: F401 — registers the repo.pushed subscriber
from conftest import make_settings
from druks.core.webhooks.github import GitHubEvents
from druks.workflows import WorkflowStartResult


def _push_payload(*, full_name, default_branch, ref, changed_paths=()):
    return {
        "ref": ref,
        "repository": {"full_name": full_name, "default_branch": default_branch},
        "commits": [
            {"added": list(changed_paths), "removed": [], "modified": []},
        ],
    }


async def _fire_push(*, payload, tmp_path):
    events = GitHubEvents(request=SimpleNamespace(), kwargs={}, settings=make_settings(tmp_path))
    events._data_cached = payload
    await events.on_push()


def _stub_profile_start(monkeypatch):
    from druks.build.workflows import Profile

    calls: list[dict] = []

    async def _start(cls, *, subject, **input):
        calls.append({"subject": subject, **input})
        return WorkflowStartResult(run_id="fake-run-id", is_duplicate=False)

    monkeypatch.setattr(Profile, "start", classmethod(_start))
    return calls


async def test_policy_push_on_default_branch_reprofiles(tmp_path, db_session, monkeypatch):
    from druks.build.models import Project, ProjectRepo

    project = Project.create(name="Acme")
    repo = ProjectRepo.create(project_id=project.id, full_name="acme/widget")
    calls = _stub_profile_start(monkeypatch)

    await _fire_push(
        payload=_push_payload(
            full_name="acme/widget",
            default_branch="main",
            ref="refs/heads/main",
            changed_paths=(".druks/build/config.yml",),
        ),
        tmp_path=tmp_path,
    )

    assert calls == [
        {
            "subject": {"type": "project_repo", "id": repo.id},
            "repo_id": repo.id,
            "refresh_only": True,
        }
    ]


async def test_non_default_branch_push_is_ignored(tmp_path, db_session, monkeypatch):
    from druks.build.models import Project, ProjectRepo

    project = Project.create(name="Acme")
    ProjectRepo.create(project_id=project.id, full_name="acme/widget")
    calls = _stub_profile_start(monkeypatch)

    await _fire_push(
        payload=_push_payload(
            full_name="acme/widget",
            default_branch="main",
            ref="refs/heads/feature-x",
            changed_paths=(".druks/build/config.yml",),
        ),
        tmp_path=tmp_path,
    )

    assert calls == []


async def test_unrelated_path_push_is_ignored(tmp_path, db_session, monkeypatch):
    from druks.build.models import Project, ProjectRepo

    project = Project.create(name="Acme")
    ProjectRepo.create(project_id=project.id, full_name="acme/widget")
    calls = _stub_profile_start(monkeypatch)

    await _fire_push(
        payload=_push_payload(
            full_name="acme/widget",
            default_branch="main",
            ref="refs/heads/main",
            changed_paths=("README.md",),
        ),
        tmp_path=tmp_path,
    )

    assert calls == []


async def test_policy_push_for_an_unknown_repo_is_a_noop(tmp_path, db_session, monkeypatch):
    calls = _stub_profile_start(monkeypatch)

    await _fire_push(
        payload=_push_payload(
            full_name="acme/not-registered",
            default_branch="main",
            ref="refs/heads/main",
            changed_paths=(".druks/build/config.yml",),
        ),
        tmp_path=tmp_path,
    )

    assert calls == []

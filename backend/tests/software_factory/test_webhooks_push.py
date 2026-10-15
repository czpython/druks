from types import SimpleNamespace

import druks.contrib.software_factory.subscribers  # noqa: F401 — registers the repo.pushed subscriber
import pytest
from druks.apps.fetcher import fetch_file
from druks.contrib.software_factory.models import Project, ProjectRepo
from druks.contrib.software_factory.workflows import Profile
from druks.core.webhooks.github import GitHubEvents
from druks.testing import make_settings


@pytest.fixture(autouse=True)
def _cache_in_tmp_path(tmp_path, monkeypatch):
    # A default-branch push evicts cached .druks files; keep them out of the real data dir.
    settings = make_settings(tmp_path)
    monkeypatch.setattr("druks.apps.fetcher.load_settings", lambda: settings)


def _push_payload(*, full_name, default_branch, ref, changed_paths=(), forced=False):
    return {
        "ref": ref,
        "forced": forced,
        "repository": {"full_name": full_name, "default_branch": default_branch},
        "commits": [
            {"added": list(changed_paths), "removed": [], "modified": []},
        ],
    }


async def _fire_push(*, payload, tmp_path):
    events = GitHubEvents(request=SimpleNamespace(), kwargs={}, settings=make_settings(tmp_path))
    events._data_cached = payload
    await events.on_push()


def _stub_profile_dispatch(monkeypatch):
    calls: list[dict] = []

    async def _dispatch(cls, repo, *, refresh_only=False):
        calls.append({"repo_id": repo.id, "refresh_only": refresh_only})
        return "fake-run-id"

    monkeypatch.setattr(Profile, "dispatch", classmethod(_dispatch))
    return calls


async def test_policy_push_on_default_branch_reprofiles(tmp_path, druks_db, monkeypatch):
    project = await Project.create(name="Acme")
    repo = await ProjectRepo.create(project_id=project.id, full_name="acme/widget")
    calls = _stub_profile_dispatch(monkeypatch)

    await _fire_push(
        payload=_push_payload(
            full_name="acme/widget",
            default_branch="main",
            ref="refs/heads/main",
            changed_paths=(".druks/software_factory/config.yml",),
        ),
        tmp_path=tmp_path,
    )

    assert calls == [
        {
            "repo_id": repo.id,
            "refresh_only": True,
        }
    ]


async def test_non_default_branch_push_is_ignored(tmp_path, druks_db, monkeypatch):
    project = await Project.create(name="Acme")
    await ProjectRepo.create(project_id=project.id, full_name="acme/widget")
    calls = _stub_profile_dispatch(monkeypatch)

    await _fire_push(
        payload=_push_payload(
            full_name="acme/widget",
            default_branch="main",
            ref="refs/heads/feature-x",
            changed_paths=(".druks/software_factory/config.yml",),
        ),
        tmp_path=tmp_path,
    )

    assert calls == []


async def test_unrelated_path_push_is_ignored(tmp_path, druks_db, monkeypatch):
    project = await Project.create(name="Acme")
    await ProjectRepo.create(project_id=project.id, full_name="acme/widget")
    calls = _stub_profile_dispatch(monkeypatch)

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


async def test_policy_push_for_an_unknown_repo_is_a_noop(tmp_path, druks_db, monkeypatch):
    calls = _stub_profile_dispatch(monkeypatch)

    await _fire_push(
        payload=_push_payload(
            full_name="acme/not-registered",
            default_branch="main",
            ref="refs/heads/main",
            changed_paths=(".druks/software_factory/config.yml",),
        ),
        tmp_path=tmp_path,
    )

    assert calls == []


@pytest.mark.parametrize(
    ("repo", "path", "forced"),
    [
        ("acme/widget", ".druks/software_factory/config.yml", False),
        ("acme/.druks", "software_factory/prompts/build/implement.md", False),
        ("acme/widget", ".druks/software_factory/config.yml", True),
    ],
)
async def test_default_branch_push_drops_the_cached_file(tmp_path, monkeypatch, repo, path, forced):
    remote = {"body": "old", "reads": 0}

    class _GitHub:
        async def get_file_content(self, repo, path):
            remote["reads"] += 1
            return remote["body"]

        async def aclose(self):
            pass

    async def _client():
        return _GitHub()

    monkeypatch.setattr("druks.core.services.Github.get_client", _client)
    announced_reads: list[str | None] = []

    async def _publish(name, **kwargs):
        announced_reads.append(await fetch_file(repo=repo, path=path))

    monkeypatch.setattr("druks.core.webhooks.github.publish", _publish)

    assert await fetch_file(repo=repo, path=path) == "old"
    remote["body"] = "new"
    await _fire_push(
        payload=_push_payload(
            full_name=repo,
            default_branch="main",
            ref="refs/heads/main",
            changed_paths=() if forced else (path,),
            forced=forced,
        ),
        tmp_path=tmp_path,
    )

    assert announced_reads == ["new"]
    assert remote["reads"] == 2

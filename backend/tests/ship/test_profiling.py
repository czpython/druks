import pytest
from druks.contrib.ship.app import Ship
from druks.contrib.ship.models import Project, ProjectRepo
from druks.contrib.ship.policy import RepoPolicy, VerificationProfile
from druks.contrib.ship.workflows import Profile
from druks.durable.engine import configure_engine
from druks.services.exceptions import ServiceNotConnectedError
from druks.services.models import ServiceIdentity
from druks.skills.datastructures import InstalledSkill
from druks.skills.models import SkillCollection


@pytest.fixture(autouse=True)
async def _passthrough_step(monkeypatch, druks_db):
    # run() is itself a durable step (single-operation workflow) — route it
    # straight through so the test needs no live DBOS runtime.
    configure_engine(await druks_db.connection())

    async def _run_step(_options, fn):
        return await fn()

    monkeypatch.setattr("druks.workflows.DBOS.run_step_async", _run_step)
    yield
    configure_engine(None)


async def _seed_repo() -> ProjectRepo:
    project = await Project.create(name="Acme")
    return await ProjectRepo.create(project_id=project.id, full_name="acme/widget")


async def _seed_skills(*names: str, disabled: tuple[str, ...] = ()) -> None:
    collection = await SkillCollection.create(
        source="test",
        name="test skills",
        skills=[
            InstalledSkill(name=name, description=f"{name} skill", path=name, content_hash="x")
            for name in names
        ],
    )
    for skill in collection.skills:
        if skill.name in disabled:
            skill.enabled = False


def _profiled(**overrides) -> dict:
    profile = {
        "languages": ["python"],
        "frameworks": ["django"],
        "package_managers": ["uv"],
        "stack_summary": "A Django backend.",
        "verification": {
            "test_commands": [{"command": "pytest", "ci_check": "Backend / tests"}],
            "lint_commands": [{"command": "ruff check .", "ci_check": "Backend / lint"}],
            "typecheck_commands": [],
        },
        "recommended_skills": ["django-patterns"],
    }
    profile.update(overrides)
    return profile


async def _no_policy(repo):
    return RepoPolicy()


@pytest.mark.parametrize("refresh_only", [False, True])
async def test_dispatch_shapes_the_profile_start(druks_db, monkeypatch, refresh_only):
    await ServiceIdentity.connect(
        "github",
        identity={"app_id": "1", "slug": "druks-operator"},
        secrets={"private_key": "operator-pem", "webhook_secret": "hook-secret"},
    )
    repo = await _seed_repo()
    calls: list[dict] = []

    async def _start(cls, **kwargs):
        calls.append(kwargs)
        return "profile-run"

    monkeypatch.setattr(Profile, "start", classmethod(_start))

    run_id = await Profile.dispatch(repo, refresh_only=refresh_only)

    assert run_id == "profile-run"
    assert calls == [{"subject": repo, "repo_id": repo.id, "refresh_only": refresh_only}]


async def test_dispatch_refuses_before_start_without_github(druks_db, monkeypatch):
    repo = await _seed_repo()

    async def _start(cls, **kwargs):
        raise AssertionError("start must not be reached without a GitHub identity")

    monkeypatch.setattr(Profile, "start", classmethod(_start))

    with pytest.raises(ServiceNotConnectedError, match="github is not connected"):
        await Profile.dispatch(repo)


class TestProfileRun:
    async def test_persists_baseline_and_effective(self, druks_db, monkeypatch):
        await _seed_skills("django-patterns")
        repo = await _seed_repo()

        async def _profiler(*, repo: str):
            return _profiled()

        monkeypatch.setattr(Ship, "repo_profiler", _profiler)
        monkeypatch.setattr(RepoPolicy, "resolve", staticmethod(_no_policy))

        await Profile().run(repo_id=repo.id)
        # The step commits on its own Session; re-fetch instead of trusting
        # the identity-mapped `repo` object across that boundary.
        repo = await ProjectRepo.get(repo.id)

        assert repo.profile["baseline"]["languages"] == ["python"]
        assert repo.effective_profile["verification"]["lint_commands"] == [
            {"command": "ruff check .", "ci_check": "Backend / lint"}
        ]

    async def test_drops_skills_that_are_not_enabled(self, druks_db, monkeypatch):
        await _seed_skills("django-patterns", "retired-skill", disabled=("retired-skill",))
        repo = await _seed_repo()

        async def _profiler(*, repo: str):
            # The agent picked a disabled skill and one that was never real.
            return _profiled(
                recommended_skills=["django-patterns", "retired-skill", "made-up-skill"]
            )

        monkeypatch.setattr(Ship, "repo_profiler", _profiler)
        monkeypatch.setattr(RepoPolicy, "resolve", staticmethod(_no_policy))

        await Profile().run(repo_id=repo.id)
        repo = await ProjectRepo.get(repo.id)

        assert repo.profile["baseline"]["recommended_skills"] == ["django-patterns"]

    async def test_pinned_verification_replaces_the_detected_one(self, druks_db, monkeypatch):
        repo = await _seed_repo()

        async def _profiler(*, repo: str):
            return _profiled()

        async def _pinning_policy(repo):
            return RepoPolicy(verification=VerificationProfile(test_commands=("make test",)))

        monkeypatch.setattr(Ship, "repo_profiler", _profiler)
        monkeypatch.setattr(RepoPolicy, "resolve", staticmethod(_pinning_policy))

        await Profile().run(repo_id=repo.id)
        repo = await ProjectRepo.get(repo.id)

        # The pin replaces the whole verification section on the effective profile...
        assert repo.effective_profile["verification"]["test_commands"] == [
            {"command": "make test", "ci_check": None}
        ]
        assert repo.effective_profile["verification"]["lint_commands"] == []
        # ...but the detected baseline is preserved underneath it.
        assert repo.profile["baseline"]["verification"]["lint_commands"] == [
            {"command": "ruff check .", "ci_check": "Backend / lint"}
        ]

    async def test_pinned_command_keeps_the_check_detected_for_it(self, druks_db, monkeypatch):
        repo = await _seed_repo()

        async def _profiler(*, repo: str):
            return _profiled()

        async def _pinning_policy(repo):
            return RepoPolicy(
                verification=VerificationProfile(test_commands=("pytest", "make e2e"))
            )

        monkeypatch.setattr(Ship, "repo_profiler", _profiler)
        monkeypatch.setattr(RepoPolicy, "resolve", staticmethod(_pinning_policy))

        await Profile().run(repo_id=repo.id)
        repo = await ProjectRepo.get(repo.id)

        assert repo.effective_profile["verification"]["test_commands"] == [
            {"command": "pytest", "ci_check": "Backend / tests"},
            {"command": "make e2e", "ci_check": None},
        ]


class TestRefreshOnly:
    async def test_skips_the_agent_and_reapplies_the_pin(self, druks_db, monkeypatch):
        repo = await _seed_repo()
        baseline = _profiled()
        await repo.set_profile(baseline=baseline, effective=baseline)

        async def _boom(*, repo: str):
            raise AssertionError("refresh_only must not call the repo profiler")

        async def _pinning_policy(repo):
            return RepoPolicy(verification=VerificationProfile(test_commands=("make test",)))

        monkeypatch.setattr(Ship, "repo_profiler", _boom)
        monkeypatch.setattr(RepoPolicy, "resolve", staticmethod(_pinning_policy))

        await Profile().run(repo_id=repo.id, refresh_only=True)
        repo = await ProjectRepo.get(repo.id)

        # Baseline untouched — only the pin re-applies.
        assert repo.profile["baseline"]["verification"]["test_commands"] == [
            {"command": "pytest", "ci_check": "Backend / tests"}
        ]
        assert repo.effective_profile["verification"]["test_commands"] == [
            {"command": "make test", "ci_check": None}
        ]
